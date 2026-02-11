"""
注文実行 Lambda
SQSからシグナルを受信し、Coincheck APIで注文実行

マルチ通貨対応:
- pair（eth_jpy, btc_jpy等）から通貨シンボルを動的に判定
- 任意の通貨ペアで買い・売りが可能
- 複数通貨同時保有OK（同じ通貨の重複購入のみブロック）
- スコアに応じた投資金額調整（期待値連動）

⚠️ Coincheck成行注文の重要な仕様:
- market_buy / market_sell のレスポンスは amount=None, rate=None
- 約定データは GET /api/exchange/orders/{id} (注文の詳細API) で取得
- 補助: /api/exchange/orders/transactions は order_id フィルタ非対応
  → レスポンスを order_id でPython側フィルタ必須
- 各fundsの値は正負が混在するため abs() で処理する
- 詳細: docs/bugfix-history.md

⚠️ SQSバッチ処理の注意点:
- handler()でraiseすると未処理レコード含むバッチ全体が再配信される
- 注文成功後にDB保存で失敗→raise→再配信→二重注文のリスク
- エラーはログ+Slack通知のみ、raiseしない設計
- _just_bought_pairs: 同一バッチ内のBUY→即SELL防止

🛑 サーキットブレーカー:
- 日次累計損失 or 連敗回数が閾値超過でBUY停止（SELLは許可）
- CIRCUIT_BREAKER_ENABLED 環境変数で ON/OFF切替
- デフォルトOFF
"""
import json
import os
import time
import math
import hmac
import hashlib
import urllib.request
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
secrets = boto3.client('secretsmanager')

POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
TRADES_TABLE = os.environ.get('TRADES_TABLE', 'eth-trading-trades')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
COINCHECK_SECRET_ARN = os.environ.get('COINCHECK_SECRET_ARN', '')
MAX_POSITION_JPY = float(os.environ.get('MAX_POSITION_JPY', '15000'))

# 通貨ペア設定（他通貨のポジションチェック用）
DEFAULT_PAIRS = {
    "eth_usdt": {"binance": "ETHUSDT", "coincheck": "eth_jpy", "news": "ETH", "name": "Ethereum"}
}
TRADING_PAIRS = json.loads(os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS)))

# 手数料設定（Coincheck取引所: 対象通貨は全て0%）
MAKER_FEE_RATE = float(os.environ.get('MAKER_FEE_RATE', '0.0'))
TAKER_FEE_RATE = float(os.environ.get('TAKER_FEE_RATE', '0.0'))

# 最小注文金額（Coincheck: 500円相当）
MIN_ORDER_JPY = float(os.environ.get('MIN_ORDER_JPY', '500'))

# Coincheck取引所: 通貨別最小注文数量・小数点以下桁数
CURRENCY_ORDER_RULES = {
    'btc': {'min_amount': 0.001, 'decimals': 8},
    'eth': {'min_amount': 0.001, 'decimals': 8},
    'xrp': {'min_amount': 1.0,   'decimals': 6},
    'sol': {'min_amount': 0.01,  'decimals': 8},
    'doge': {'min_amount': 1.0,  'decimals': 2},
    'avax': {'min_amount': 0.01, 'decimals': 8},
}

# 予備資金（常に残しておく金額）
RESERVE_JPY = float(os.environ.get('RESERVE_JPY', '1000'))

# サーキットブレーカー設定
CIRCUIT_BREAKER_ENABLED = os.environ.get('CIRCUIT_BREAKER_ENABLED', 'false').lower() == 'true'
CB_DAILY_LOSS_LIMIT_JPY = float(os.environ.get('CB_DAILY_LOSS_LIMIT_JPY', '15000'))   # 日次累計損失上限（資金の約12%）
CB_MAX_CONSECUTIVE_LOSSES = int(os.environ.get('CB_MAX_CONSECUTIVE_LOSSES', '5'))      # 連敗上限
CB_COOLDOWN_HOURS = float(os.environ.get('CB_COOLDOWN_HOURS', '6'))                    # トリップ後の冷却時間

# Kelly Criterion ベースの投資比率（期待値連動）
# 過去のトレード統計から勝率・損益比を計算し、最適な投資比率を算出
# データ不足時（5件未満）はフォールバック固定比率を使用
# フォールバック比率は Half-Kelly 相当の保守的設定
FALLBACK_SCORE_THRESHOLDS = [
    (0.45, 0.60),   # スコア0.45以上 → 利用可能残高の60%（非常に強いシグナル）
    (0.35, 0.45),   # スコア0.35-0.45 → 45%（強いシグナル）
    (0.25, 0.30),   # スコア0.25-0.35 → 30%（中程度のシグナル）
    (0.15, 0.20),   # スコア0.15-0.25 → 20%（弱いシグナル）
]
# Kelly計算に必要な最少トレード件数
MIN_TRADES_FOR_KELLY = int(os.environ.get('MIN_TRADES_FOR_KELLY', '5'))
# Kelly fraction の安全マージン（0.5 = Half-Kelly）
KELLY_SAFETY_FACTOR = float(os.environ.get('KELLY_SAFETY_FACTOR', '0.5'))
# Kelly fraction のクランプ範囲
KELLY_MIN_FRACTION = 0.10  # 最低10%
KELLY_MAX_FRACTION = 0.80  # 最大80%


def get_currency_from_pair(pair: str) -> str:
    """Coincheckペアから通貨シンボルを取得（例: eth_jpy → eth）"""
    return pair.split('_')[0]


def get_currency_name(pair: str) -> str:
    """ペアから表示名を取得"""
    currency = get_currency_from_pair(pair)
    for config in TRADING_PAIRS.values():
        if config['coincheck'] == pair:
            return config['name']
    return currency.upper()


# 同一Lambda呼び出し内で買った通貨を追跡（バッチ内即売り防止）
# SQSバッチにBUY+SELLが同居すると、BUY直後にSELLが実行される問題の対策
# execute_buy()成功時にペアを追加、process_order()のSELL分岐でチェック
_just_bought_pairs = set()


def handler(event, context):
    """注文実行"""
    global _just_bought_pairs
    _just_bought_pairs = set()
    errors = []

    for record in event.get('Records', []):
        try:
            body = json.loads(record['body'])
            process_order(body)
        except Exception as e:
            print(f"Error processing order: {str(e)}")
            import traceback
            traceback.print_exc()
            errors.append(str(e))
            # ⚠️ 絶対にraiseしない（SQSバッチ再配信→二重注文防止）
            # Coincheck注文APIは成功したがDB保存で例外 → raiseすると
            # SQSがバッチ全体を再配信 → 同じ注文がもう一度実行される
            # 代わりにSlack通知で人間に知らせる
            send_notification('System', f'❌ 注文処理エラー\n{str(e)}')

    if errors:
        print(f"Completed with {len(errors)} error(s): {errors}")

    return {'statusCode': 200, 'body': 'OK'}


def process_order(order: dict):
    """注文処理"""
    pair = order['pair']
    signal = order['signal']
    score = order['score']
    analysis_context = order.get('analysis_context', {})
    currency = get_currency_from_pair(pair)
    name = get_currency_name(pair)

    # 1. 現在のポジション確認
    current_position = get_position(pair)

    # 2. 注文判定
    if signal == 'BUY':
        if current_position and current_position.get('side') == 'long':
            print(f"Already have long position for {pair}")
            return

        # サーキットブレーカーチェック（BUYのみブロック、SELLは常に許可）
        if CIRCUIT_BREAKER_ENABLED:
            tripped, reason = check_circuit_breaker()
            if tripped:
                print(f"Circuit breaker TRIPPED: {reason}")
                send_notification(
                    name,
                    f"🛑 サーキットブレーカー発動\n"
                    f"通貨: {name}\n"
                    f"理由: {reason}\n"
                    f"BUY注文をブロックしました"
                )
                return

        # 買い注文
        execute_buy(pair, score, analysis_context)

    elif signal == 'SELL':
        if not current_position or current_position.get('side') != 'long':
            print(f"No long position to sell for {pair}")
            return

        # 同一バッチ内で買ったばかりの通貨は売らない（BUY→即SELL防止）
        if pair in _just_bought_pairs:
            print(f"Skipping sell for {pair}: just bought in this batch")
            send_notification(
                name,
                f"⚠️ {name}売りスキップ\n"
                f"理由: 同一実行内でBUY直後のため"
            )
            return

        # 売り注文
        execute_sell(pair, current_position, score, analysis_context)


def get_position(pair: str) -> dict:
    """現在のポジション取得"""
    table = dynamodb.Table(POSITIONS_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=1
    )
    items = response.get('Items', [])
    if items and not items[0].get('closed'):
        return items[0]
    return None


def check_any_other_position(exclude_pair: str) -> dict:
    """指定ペア以外にアクティブポジションがないかチェック"""
    table = dynamodb.Table(POSITIONS_TABLE)

    for config in TRADING_PAIRS.values():
        coincheck_pair = config['coincheck']
        if coincheck_pair == exclude_pair:
            continue

        try:
            response = table.query(
                KeyConditionExpression='pair = :pair',
                ExpressionAttributeValues={':pair': coincheck_pair},
                ScanIndexForward=False,
                Limit=1
            )
            items = response.get('Items', [])
            if items and not items[0].get('closed'):
                return items[0]
        except Exception as e:
            print(f"Error checking position for {coincheck_pair}: {e}")

    return None


def get_balance() -> dict:
    """Coincheck APIで残高取得"""
    try:
        creds = get_api_credentials()
        if not creds:
            print("No API credentials for balance check")
            return {'jpy': 0}

        result = call_coincheck_api('/api/accounts/balance', 'GET', None, creds)

        if result and result.get('success'):
            # 全通貨の残高を返す
            balance = {
                'jpy': float(result.get('jpy', 0)),
                'jpy_reserved': float(result.get('jpy_reserved', 0))
            }
            # 各暗号通貨の残高も取得
            for config in TRADING_PAIRS.values():
                currency = get_currency_from_pair(config['coincheck'])
                balance[currency] = float(result.get(currency, 0))
                balance[f'{currency}_reserved'] = float(result.get(f'{currency}_reserved', 0))
            return balance
        else:
            print(f"Balance API error: {result}")
            return {'jpy': 0}

    except Exception as e:
        print(f"Error getting balance: {str(e)}")
        return {'jpy': 0}


def get_trade_statistics() -> dict:
    """
    過去90日間のクローズ済みポジションから勝率・平均損益率を計算
    Kelly Criterion の入力パラメータを生成

    Returns:
        {
            'total_trades': int,
            'win_rate': float (0-1),
            'avg_win_pct': float (例: 3.5 = +3.5%),
            'avg_loss_pct': float (例: 2.0 = -2.0%, 絶対値),
            'n_wins': int,
            'n_losses': int,
        }
    """
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        now = int(time.time())
        cutoff = now - (90 * 86400)  # 過去90日

        closed_positions = []
        for config in TRADING_PAIRS.values():
            coincheck_pair = config['coincheck']
            try:
                response = table.query(
                    KeyConditionExpression='pair = :pair',
                    ExpressionAttributeValues={':pair': coincheck_pair}
                )
                items = response.get('Items', [])
                for item in items:
                    if item.get('closed') and item.get('exit_time') and item.get('exit_price'):
                        exit_time = int(item.get('exit_time', 0))
                        if exit_time > cutoff:
                            entry_price = float(item.get('entry_price', 0))
                            exit_price = float(item.get('exit_price', 0))
                            if entry_price > 0:
                                pnl_pct = (exit_price - entry_price) / entry_price * 100
                                closed_positions.append(pnl_pct)
            except Exception as e:
                print(f"Trade stats: error querying {coincheck_pair}: {e}")

        if not closed_positions:
            return {'total_trades': 0}

        wins = [p for p in closed_positions if p > 0]
        losses = [abs(p) for p in closed_positions if p <= 0]

        return {
            'total_trades': len(closed_positions),
            'win_rate': len(wins) / len(closed_positions),
            'avg_win_pct': sum(wins) / len(wins) if wins else 0,
            'avg_loss_pct': sum(losses) / len(losses) if losses else 0,
            'n_wins': len(wins),
            'n_losses': len(losses),
        }
    except Exception as e:
        print(f"Error getting trade statistics: {e}")
        return {'total_trades': 0}


def calculate_order_amount(score: float, available_jpy: float) -> float:
    """
    Kelly Criterion ベースの投資金額計算（期待値最大化）

    ロジック:
    1. 過去90日のクローズ済みポジションから勝率・損益比を算出
    2. Kelly fraction = (p × b - q) / b
       p=勝率, q=1-p, b=平均勝ち/平均負け
    3. Half-Kelly（安全マージン50%）を適用
    4. スコアで変調（高スコア → Kelly寄り、低スコア → 保守的）
    5. データ不足時はフォールバック（スコアベース固定比率）

    利点:
    - 勝率が高い時は自動的に投資比率が増加
    - 負けが続くと自動的に投資比率が低下（破産確率を最小化）
    - 期待値がプラスの時のみ有意な投資を行う
    """
    # 1. 過去のトレード統計を取得
    stats = get_trade_statistics()

    if stats['total_trades'] < MIN_TRADES_FOR_KELLY:
        print(f"Insufficient trade history ({stats['total_trades']} trades < {MIN_TRADES_FOR_KELLY}), "
              f"using fallback sizing")
        return _calculate_order_amount_fallback(score, available_jpy)

    win_rate = stats['win_rate']
    avg_win_pct = stats['avg_win_pct']
    avg_loss_pct = stats['avg_loss_pct']

    print(f"Trade stats: {stats['total_trades']} trades "
          f"(W:{stats['n_wins']}/L:{stats['n_losses']}), "
          f"win_rate={win_rate:.2f}, avg_win={avg_win_pct:+.2f}%, avg_loss=-{avg_loss_pct:.2f}%")

    if avg_loss_pct == 0:
        print("No losing trades (avg_loss=0), using fallback")
        return _calculate_order_amount_fallback(score, available_jpy)

    # 2. Kelly fraction 計算
    # f* = (p × b - q) / b
    b = avg_win_pct / avg_loss_pct  # win/loss ratio
    q = 1 - win_rate

    kelly_full = (win_rate * b - q) / b

    if kelly_full <= 0:
        # 負のKelly → エッジがない（期待値マイナス）
        # 最低限のポジションのみ取る（様子見）
        print(f"Negative Kelly ({kelly_full:.4f}): no positive edge, using minimum fraction")
        kelly_fraction = KELLY_MIN_FRACTION
    else:
        # Half-Kelly for safety（破産リスクを大幅に低減）
        kelly_fraction = kelly_full * KELLY_SAFETY_FACTOR

    # 3. スコアによる変調
    # BUY閾値付近(≈0.25)のスコアは控えめ、高スコアはKelly寄り
    # score=0.15 → factor=0.3, score=0.25 → factor=0.5, score=0.50 → factor=1.0
    score_factor = min(1.0, max(0.3, (score - 0.10) / 0.40))
    adjusted_fraction = kelly_fraction * score_factor

    # 4. クランプ（最低10%、最大80%）
    adjusted_fraction = max(KELLY_MIN_FRACTION, min(KELLY_MAX_FRACTION, adjusted_fraction))

    print(f"Kelly sizing: full_kelly={kelly_full:.4f}, half_kelly={kelly_fraction:.4f}, "
          f"score_factor={score_factor:.2f}, adjusted={adjusted_fraction:.4f}")

    # 5. 投資金額計算
    order_amount = available_jpy * adjusted_fraction

    # 手数料を考慮
    if TAKER_FEE_RATE > 0:
        order_amount = order_amount / (1 + TAKER_FEE_RATE)

    # 上限・下限チェック
    order_amount = min(order_amount, MAX_POSITION_JPY)

    if order_amount < MIN_ORDER_JPY:
        print(f"Order amount ¥{order_amount:,.0f} below minimum ¥{MIN_ORDER_JPY:,.0f}")
        return 0

    return order_amount


def _calculate_order_amount_fallback(score: float, available_jpy: float) -> float:
    """
    フォールバック: スコアベースの固定比率（Kelly計算不可時）
    Half-Kelly相当の保守的な設定
    """
    ratio = 0.0
    for threshold, r in FALLBACK_SCORE_THRESHOLDS:
        if score >= threshold:
            ratio = r
            break

    if ratio == 0:
        print(f"Score {score} below minimum threshold, skipping order")
        return 0

    # 投資金額計算
    order_amount = available_jpy * ratio

    # 手数料を考慮
    if TAKER_FEE_RATE > 0:
        order_amount = order_amount / (1 + TAKER_FEE_RATE)

    # 上限・下限チェック
    order_amount = min(order_amount, MAX_POSITION_JPY)

    if order_amount < MIN_ORDER_JPY:
        print(f"Order amount ¥{order_amount:,.0f} below minimum ¥{MIN_ORDER_JPY:,.0f}")
        return 0

    return order_amount


def execute_buy(pair: str, score: float, analysis_context: dict = None):
    """買い注文実行（残高確認・スコア連動金額）"""
    timestamp = int(time.time())
    name = get_currency_name(pair)

    # 1. 残高確認
    balance = get_balance()
    available_jpy = balance.get('jpy', 0) - balance.get('jpy_reserved', 0) - RESERVE_JPY

    print(f"Balance: ¥{balance.get('jpy', 0):,.0f} "
          f"(reserved: ¥{balance.get('jpy_reserved', 0):,.0f})")
    print(f"Available for trading: ¥{available_jpy:,.0f} "
          f"(after reserve: ¥{RESERVE_JPY:,.0f})")

    if available_jpy <= 0:
        print("Insufficient JPY balance")
        send_notification(name, f"⚠️ 残高不足\n利用可能残高: ¥{available_jpy:,.0f}")
        return

    # 2. スコアに応じた投資金額計算
    order_amount = calculate_order_amount(score, available_jpy)

    if order_amount <= 0:
        print(f"Order amount is 0 (score: {score}, available: ¥{available_jpy:,.0f})")
        return

    print(f"Order amount: ¥{order_amount:,.0f} (score: {score:.3f}, "
          f"ratio: {order_amount/available_jpy*100:.1f}%)")

    # 2.5. 既に暗号通貨を保有していないかチェック（SQSリトライによる重複購入防止）
    currency = get_currency_from_pair(pair)
    crypto_balance = balance.get(currency, 0)
    if crypto_balance > 0:
        rules = CURRENCY_ORDER_RULES.get(currency, {'min_amount': 0.001, 'decimals': 8})
        if crypto_balance >= rules['min_amount']:
            print(f"Already holding {crypto_balance} {currency.upper()}, skipping duplicate buy")
            send_notification(
                name,
                f"⚠️ {name}重複購入をブロック\n"
                f"既に {crypto_balance:.6f} {currency.upper()} を保有中"
            )
            return

    # 3. Coincheck APIで成行買い
    result = place_market_order(pair, 'buy', order_amount)

    if result and result.get('success'):
        order_id = result.get('id')

        # 成行買いはamount/rateがNoneで返るため、約定情報を取得
        fill_amount, fill_rate = get_market_buy_fill(pair, order_id, currency)

        # 約定情報で result を補完
        if fill_amount and fill_rate:
            result['amount'] = fill_amount
            result['rate'] = fill_rate
        else:
            # フォールバック: 残高差分から推定
            new_balance = get_balance()
            new_crypto = new_balance.get(currency, 0)
            estimated_amount = new_crypto - crypto_balance
            estimated_rate = order_amount / estimated_amount if estimated_amount > 0 else 0
            result['amount'] = estimated_amount if estimated_amount > 0 else 0
            result['rate'] = estimated_rate
            print(f"Fill info unavailable, estimated: amount={estimated_amount}, rate={estimated_rate}")

        # ⚠️ entry_price 妥当性チェック（ticker価格と比較）
        # fill取得バグで entry_price が桁違いに膨張すると即SL発動→資金溶解
        entry_rate = float(result.get('rate', 0))
        if entry_rate > 0:
            try:
                ticker_price = get_current_price(pair)
                if ticker_price > 0:
                    deviation = abs(entry_rate - ticker_price) / ticker_price
                    if deviation > 0.5:  # 50%以上の乖離は明らかに異常
                        print(f"⚠️ CRITICAL: entry_price ¥{entry_rate:,.0f} deviates "
                              f"{deviation*100:.1f}% from ticker ¥{ticker_price:,.0f}. "
                              f"Using ticker price as fallback")
                        send_notification(
                            name,
                            f"⚠️ {name}約定価格異常検知\n"
                            f"取得値: ¥{entry_rate:,.0f}\n"
                            f"Ticker: ¥{ticker_price:,.0f}\n"
                            f"乖離: {deviation*100:.1f}%\n"
                            f"→ Ticker価格で代替"
                        )
                        result['rate'] = ticker_price
                        # 実際の購入数量も再計算
                        result['amount'] = order_amount / ticker_price
            except Exception as e:
                print(f"Ticker sanity check failed: {e}")

        # ポジション保存
        save_position(pair, timestamp, 'long', result, order_amount)

        # 取引履歴保存（分析コンテキスト付き）
        save_trade(pair, timestamp, 'BUY', result, analysis_context=analysis_context)

        # 同一バッチ内即売り防止フラグ
        _just_bought_pairs.add(pair)

        # 通知
        ratio_pct = (order_amount / available_jpy) * 100
        fill_info = f"\n数量: {result.get('amount', 0):.6f} {currency.upper()}" if result.get('amount') else ""
        send_notification(
            name,
            f"🟢 {name}買い約定\n"
            f"通貨ペア: {pair}\n"
            f"金額: ¥{order_amount:,.0f} ({ratio_pct:.0f}%){fill_info}\n"
            f"スコア: {score:.3f}\n"
            f"残高: ¥{available_jpy - order_amount:,.0f}"
        )
    else:
        error_msg = result.get('error', 'Unknown error') if result else 'API call failed'
        print(f"Buy order failed: {error_msg}")
        send_notification(name, f"❌ {name}買い注文失敗\nエラー: {error_msg}")


def execute_sell(pair: str, position: dict, score: float, analysis_context: dict = None):
    """売り注文実行"""
    timestamp = int(time.time())
    currency = get_currency_from_pair(pair)
    name = get_currency_name(pair)

    amount = float(position.get('amount', 0))
    if amount <= 0:
        print(f"No {currency.upper()} amount in position")
        return

    # 通貨別の最小注文数量・小数点桁数チェック
    rules = CURRENCY_ORDER_RULES.get(currency, {'min_amount': 0.001, 'decimals': 8})
    decimals = rules['decimals']
    min_amount = rules['min_amount']

    # 小数点以下を適切な桁数に切り捨て（切り上げると残高不足になる）
    amount = math.floor(amount * (10 ** decimals)) / (10 ** decimals)

    if amount < min_amount:
        print(f"{currency.upper()} amount {amount} below minimum {min_amount}")
        send_notification(
            name,
            f"⚠️ {currency.upper()}売りスキップ: 最小注文数量未満\n"
            f"保有: {amount} {currency.upper()}\n"
            f"最小: {min_amount} {currency.upper()}"
        )
        return

    # 残高確認
    balance = get_balance()
    available = balance.get(currency, 0) - balance.get(f'{currency}_reserved', 0)

    if available < amount:
        print(f"{currency.upper()} balance mismatch: position={amount}, available={available}")
        amount = available
        # 再度小数点丸め・最小数量チェック
        amount = math.floor(amount * (10 ** decimals)) / (10 ** decimals)
        if amount < min_amount:
            send_notification(
                name,
                f"⚠️ {currency.upper()}残高不足\n保有: {available:.6f} {currency.upper()}"
            )
            return

    # 売り前の暗号通貨残高を記録（フォールバック推定用）
    pre_sell_crypto = balance.get(currency, 0)

    # Coincheck APIで成行売り
    result = place_market_order(pair, 'sell', amount_crypto=amount)

    if result and result.get('success'):
        order_id = result.get('id')

        # 成行売りの rate は Coincheck API レスポンスで信頼できないため
        # 必ず約定履歴から取得する
        sell_rate = None
        sell_amount = result.get('amount')

        fill_amount, fill_rate = get_market_sell_fill(pair, order_id, currency)
        if fill_rate:
            sell_rate = fill_rate
            result['rate'] = fill_rate
        if fill_amount:
            sell_amount = fill_amount
            result['amount'] = fill_amount

        # それでもrateが取れない場合、現在価格から推定
        if not sell_rate:
            try:
                import urllib.request as _ur
                ticker = json.loads(_ur.urlopen(f'https://coincheck.com/api/ticker?pair={pair}', timeout=5).read())
                sell_rate = float(ticker.get('last', 0))
                result['rate'] = sell_rate
                print(f"Sell rate unavailable, using ticker price: {sell_rate}")
            except Exception as e:
                print(f"Ticker fallback failed: {e}")
                sell_rate = 0

        # ⚠️ sell_rate 妥当性チェック（ticker価格と比較）
        if sell_rate and float(sell_rate) > 0:
            try:
                ticker_price = get_current_price(pair)
                if ticker_price > 0:
                    sell_rate_f = float(sell_rate)
                    deviation = abs(sell_rate_f - ticker_price) / ticker_price
                    if deviation > 0.15:  # 15%以上の乖離は異常
                        print(f"⚠️ CRITICAL: sell_rate ¥{sell_rate_f:,.0f} deviates "
                              f"{deviation*100:.1f}% from ticker ¥{ticker_price:,.0f}. "
                              f"Using ticker price as fallback")
                        send_notification(
                            name,
                            f"⚠️ {name}売却価格異常検知\n"
                            f"取得値: ¥{sell_rate_f:,.0f}\n"
                            f"Ticker: ¥{ticker_price:,.0f}\n"
                            f"乖離: {deviation*100:.1f}%\n"
                            f"→ Ticker価格で代替"
                        )
                        sell_rate = ticker_price
                        result['rate'] = ticker_price
            except Exception as e:
                print(f"Sell rate sanity check failed: {e}")

        # ポジションクローズ
        close_position(pair, position, timestamp, result)

        # 取引履歴保存（分析コンテキスト付き）
        save_trade(pair, timestamp, 'SELL', result, analysis_context=analysis_context)

        # P/L計算
        entry_price = float(position.get('entry_price', 0))
        try:
            exit_price = float(sell_rate) if sell_rate else 0
        except (TypeError, ValueError):
            exit_price = 0

        gross_pnl = (exit_price - entry_price) * amount

        sell_fee = exit_price * amount * TAKER_FEE_RATE
        net_pnl = gross_pnl - sell_fee

        emoji = '💰' if net_pnl > 0 else '💸'
        fee_info = f"\n手数料: ¥{sell_fee:,.0f}" if sell_fee > 0 else ""
        pnl_text = f"¥{net_pnl:,.0f}" if exit_price > 0 else "不明（約定価格取得失敗）"
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        send_notification(
            name,
            f"{emoji} {name}売り約定\n"
            f"通貨ペア: {pair}\n"
            f"数量: {amount:.6f} {currency.upper()}\n"
            f"約定価格: ¥{exit_price:,.0f} (参入: ¥{entry_price:,.0f})\n"
            f"P/L: {pnl_text} ({pnl_pct:+.2f}%){fee_info}\n"
            f"スコア: {score:.3f}"
        )
    else:
        error_msg = result.get('error', 'Unknown error') if result else 'API call failed'
        print(f"Sell order failed: {error_msg}")
        send_notification(name, f"❌ {name}売り注文失敗\nエラー: {error_msg}")


def place_market_order(pair: str, side: str, amount_jpy: float = None, amount_crypto: float = None) -> dict:
    """成行注文（Coincheck API）"""
    try:
        creds = get_api_credentials()
        if not creds:
            print("No API credentials found")
            return {'success': False, 'error': 'no_credentials'}

        params = {
            'pair': pair,
            'order_type': f'market_{side}'
        }

        if side == 'buy' and amount_jpy:
            params['market_buy_amount'] = str(amount_jpy)
        elif side == 'sell' and amount_crypto:
            params['amount'] = str(amount_crypto)

        print(f"Placing order: {params}")

        result = call_coincheck_api('/api/exchange/orders', 'POST', params, creds)
        print(f"Order result: {result}")
        return result

    except Exception as e:
        print(f"Order error: {str(e)}")
        return {'success': False, 'error': str(e)}


def get_current_price(pair: str) -> float:
    """Coincheck APIから現在の取引価格を取得（JPY）"""
    url = f"https://coincheck.com/api/ticker?pair={pair}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        return float(data['last'])


def get_api_credentials() -> dict:
    """Secrets Managerからクレデンシャル取得"""
    if not COINCHECK_SECRET_ARN:
        return None

    try:
        response = secrets.get_secret_value(SecretId=COINCHECK_SECRET_ARN)
        return json.loads(response['SecretString'])
    except:
        return None


def call_coincheck_api(path: str, method: str, params: dict, creds: dict) -> dict:
    """Coincheck API呼び出し"""
    base_url = 'https://coincheck.com'
    nonce = str(int(time.time() * 1000000))

    if method == 'GET':
        body = ''
    else:
        body = json.dumps(params) if params else ''

    message = nonce + base_url + path + body

    signature = hmac.new(
        creds['secret_key'].encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        'ACCESS-KEY': creds['access_key'],
        'ACCESS-NONCE': nonce,
        'ACCESS-SIGNATURE': signature,
        'Content-Type': 'application/json'
    }

    req = urllib.request.Request(
        base_url + path,
        data=body.encode() if body else None,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())


def get_market_buy_fill(pair: str, order_id, currency: str, max_retries: int = 3) -> tuple:
    """
    成行買いの約定情報を取得

    ⚠️ 重要: Coincheck API の仕様
    - GET /api/exchange/orders/transactions は order_id クエリパラメータ非対応
      → レスポンスに全注文のトランザクションが混在する
      → Python側で order_id フィルタ必須（フィルタなしだと全トランザクション合算で
        entry_price が桁違いに膨張し、即座にSTOP_LOSS発動→資金溶解）

    取得順序:
    1. GET /api/exchange/orders/{id} (注文の詳細 — executed_amount/executed_market_buy_amount)
    2. フォールバック: transactions API + order_id フィルタ
    """
    if not order_id:
        return None, None

    creds = get_api_credentials()
    if not creds:
        return None, None

    for attempt in range(max_retries):
        time.sleep(2 * (attempt + 1))  # 2秒, 4秒, 6秒待機
        try:
            # === 方法1: 注文の詳細API (最も信頼性が高い) ===
            order_detail = call_coincheck_api(
                f'/api/exchange/orders/{order_id}',
                'GET', None, creds
            )

            if order_detail and order_detail.get('success'):
                executed_amount = float(order_detail.get('executed_amount') or 0)
                executed_jpy = float(order_detail.get('executed_market_buy_amount') or 0)

                if executed_amount > 0 and executed_jpy > 0:
                    avg_rate = executed_jpy / executed_amount
                    print(f"Fill data from order detail API (attempt {attempt+1}): "
                          f"amount={executed_amount}, rate={avg_rate:.2f}, "
                          f"jpy={executed_jpy:.0f}, status={order_detail.get('status')}")
                    return executed_amount, avg_rate
                else:
                    print(f"Order detail API: order not yet filled "
                          f"(executed_amount={executed_amount}, status={order_detail.get('status')})")

            # === 方法2: トランザクションAPI + order_id フィルタ ===
            result = call_coincheck_api(
                '/api/exchange/orders/transactions?limit=100',
                'GET', None, creds
            )

            if result and result.get('success') and result.get('transactions'):
                # ⚠️ CRITICAL: order_id でフィルタ必須
                # この API は order_id クエリパラメータ非対応のため、
                # フィルタしないと全注文のトランザクションが合算されて
                # entry_price が桁違いに膨張する
                transactions = [
                    t for t in result['transactions']
                    if str(t.get('order_id')) == str(order_id)
                ]

                if not transactions:
                    print(f"No transactions found for order_id={order_id} "
                          f"(total transactions returned: {len(result['transactions'])})")
                    continue

                total_amount = sum(abs(float(t.get('funds', {}).get(currency, 0))) for t in transactions)
                total_jpy = sum(abs(float(t.get('funds', {}).get('jpy', 0))) for t in transactions)

                if total_amount > 0:
                    avg_rate = total_jpy / total_amount
                    print(f"Fill data from transactions API (attempt {attempt+1}): "
                          f"amount={total_amount}, rate={avg_rate:.2f}, "
                          f"txn_count={len(transactions)} (filtered from {len(result['transactions'])})")
                    return total_amount, avg_rate

            print(f"Fill data not ready yet (attempt {attempt+1})")
        except Exception as e:
            print(f"Error fetching fill data (attempt {attempt+1}): {e}")

    print("Could not retrieve fill data after retries")
    return None, None


def get_market_sell_fill(pair: str, order_id, currency: str, max_retries: int = 3) -> tuple:
    """
    成行売りの約定情報を取得
    Coincheckの成行売りレスポンスもrateがNoneになることがあるため、
    約定後に取引履歴APIで実際の約定価格を取得する

    ⚠️ 注意事項:
    - 注文詳細APIの rate は成行売りでは信頼できない（null or 不正確）
    - executed_market_buy_amount は買い専用で売りには存在しない
    - → トランザクションAPIから JPY/数量 で正確な平均約定価格を算出
    - transactions API は order_id クエリパラメータ非対応
      → Python側で order_id フィルタ必須
    """
    if not order_id:
        return None, None

    creds = get_api_credentials()
    if not creds:
        return None, None

    for attempt in range(max_retries):
        time.sleep(2 * (attempt + 1))  # 2秒, 4秒, 6秒待機
        try:
            # === 方法1: 注文の詳細API（約定完了確認のみ） ===
            order_detail = call_coincheck_api(
                f'/api/exchange/orders/{order_id}',
                'GET', None, creds
            )

            if order_detail and order_detail.get('success'):
                executed_amount = float(order_detail.get('executed_amount') or 0)
                status = order_detail.get('status')
                # ⚠️ 成行売りの rate は信頼できないため使わない
                # （executed_market_buy_amount は買い専用で売りには存在しない）
                # 約定完了を確認したらトランザクションAPIで正確な価格を取得
                if executed_amount > 0:
                    print(f"Sell order confirmed filled (attempt {attempt+1}): "
                          f"executed_amount={executed_amount}, status={status}")
                else:
                    print(f"Sell order not yet filled (attempt {attempt+1}): "
                          f"executed_amount={executed_amount}, status={status}")
                    continue  # 未約定なら次のリトライへ

            # === 方法2: トランザクションAPI + order_id フィルタ（正確な約定価格） ===
            result = call_coincheck_api(
                '/api/exchange/orders/transactions?limit=100',
                'GET', None, creds
            )

            if result and result.get('success') and result.get('transactions'):
                # ⚠️ CRITICAL: order_id でフィルタ必須
                transactions = [
                    t for t in result['transactions']
                    if str(t.get('order_id')) == str(order_id)
                ]

                if not transactions:
                    print(f"No sell transactions for order_id={order_id} "
                          f"(total: {len(result['transactions'])})")
                    continue

                total_amount = sum(abs(float(t.get('funds', {}).get(currency, 0))) for t in transactions)
                total_jpy = sum(abs(float(t.get('funds', {}).get('jpy', 0))) for t in transactions)

                if total_amount > 0 and total_jpy > 0:
                    avg_rate = total_jpy / total_amount
                    print(f"Sell fill from transactions API (attempt {attempt+1}): "
                          f"amount={total_amount}, rate={avg_rate:.2f}, "
                          f"jpy={total_jpy:.0f}, txn_count={len(transactions)}")
                    return total_amount, avg_rate

            print(f"Sell fill data not ready yet (attempt {attempt+1})")
        except Exception as e:
            print(f"Error fetching sell fill data (attempt {attempt+1}): {e}")

    print("Could not retrieve sell fill data after retries")
    return None, None


def save_position(pair: str, timestamp: int, side: str, result: dict, order_amount_jpy: float = None):
    """ポジション保存"""
    table = dynamodb.Table(POSITIONS_TABLE)

    amount = result.get('amount') or 0
    rate = result.get('rate') or 0

    # None や無効な値をフロートに変換（Decimalクラッシュ防止）
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 0

    if amount <= 0 or rate <= 0:
        print(f"WARNING: Saving position with incomplete fill data: amount={amount}, rate={rate}")

    table.put_item(Item={
        'pair': pair,
        'position_id': f"{timestamp}",
        'side': side,
        'amount': Decimal(str(amount)),
        'entry_price': Decimal(str(rate)),
        'entry_time': timestamp,
        'order_amount_jpy': Decimal(str(order_amount_jpy or 0)),
        'stop_loss': Decimal(str(rate * 0.95)),
        'take_profit': Decimal(str(rate * 1.10)),
        'closed': False
    })


def close_position(pair: str, position: dict, timestamp: int, result: dict):
    """ポジションクローズ"""
    table = dynamodb.Table(POSITIONS_TABLE)

    exit_rate = result.get('rate') or 0
    try:
        exit_rate = float(exit_rate)
    except (TypeError, ValueError):
        exit_rate = 0

    table.update_item(
        Key={'pair': pair, 'position_id': position['position_id']},
        UpdateExpression='SET closed = :closed, exit_price = :exit, exit_time = :time',
        ExpressionAttributeValues={
            ':closed': True,
            ':exit': Decimal(str(exit_rate)),
            ':time': timestamp
        }
    )


def save_trade(pair: str, timestamp: int, action: str, result: dict,
               analysis_context: dict = None):
    """取引履歴保存（分析コンテキスト付き）"""
    table = dynamodb.Table(TRADES_TABLE)

    amount = result.get('amount') or 0
    rate = result.get('rate') or 0
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 0

    item = {
        'pair': pair,
        'timestamp': timestamp,
        'action': action,
        'amount': Decimal(str(amount)),
        'rate': Decimal(str(rate)),
        'order_id': str(result.get('id', '')),
        'fee_rate': Decimal(str(TAKER_FEE_RATE)),
        'ttl': timestamp + (90 * 86400)  # 90日後に自動削除
    }

    # 分析コンテキストを保存（事後分析用）
    if analysis_context:
        components = analysis_context.get('components', {})
        if components:
            if 'technical' in components:
                item['technical_score'] = Decimal(str(components['technical']))
            if 'chronos' in components:
                item['chronos_score'] = Decimal(str(components['chronos']))
            if 'sentiment' in components:
                item['sentiment_score'] = Decimal(str(components['sentiment']))
        weights = analysis_context.get('weights', {})
        if weights:
            item['weight_technical'] = Decimal(str(weights.get('technical', 0)))
            item['weight_chronos'] = Decimal(str(weights.get('chronos', 0)))
            item['weight_sentiment'] = Decimal(str(weights.get('sentiment', 0)))
        if 'buy_threshold' in analysis_context:
            item['buy_threshold'] = Decimal(str(analysis_context['buy_threshold']))
        if 'sell_threshold' in analysis_context:
            item['sell_threshold'] = Decimal(str(analysis_context['sell_threshold']))

    table.put_item(Item=item)


def check_circuit_breaker() -> tuple:
    """
    サーキットブレーカー判定

    2つの条件のいずれかでトリップ:
    1. 日次累計損失が CB_DAILY_LOSS_LIMIT_JPY を超過
    2. 直近の連敗回数が CB_MAX_CONSECUTIVE_LOSSES を超過

    Returns:
        (tripped: bool, reason: str)
    """
    try:
        table = dynamodb.Table(POSITIONS_TABLE)
        now = int(time.time())
        today_start = now - 86400  # 24時間前

        closed_positions = []

        # 全通貨ペアのクローズ済みポジションを収集
        for config in TRADING_PAIRS.values():
            coincheck_pair = config['coincheck']
            try:
                response = table.query(
                    KeyConditionExpression='pair = :pair',
                    ExpressionAttributeValues={':pair': coincheck_pair}
                )
                items = response.get('Items', [])
                for item in items:
                    if item.get('closed') and item.get('exit_time') and item.get('exit_price'):
                        exit_time = int(item.get('exit_time', 0))
                        if exit_time > today_start:
                            entry_price = float(item.get('entry_price', 0))
                            exit_price = float(item.get('exit_price', 0))
                            amount = float(item.get('amount', 0))
                            pnl = (exit_price - entry_price) * amount
                            closed_positions.append({
                                'exit_time': exit_time,
                                'pnl': pnl,
                                'pair': coincheck_pair
                            })
            except Exception as e:
                print(f"Circuit breaker: error querying {coincheck_pair}: {e}")

        if not closed_positions:
            return False, ""

        # 時系列ソート（古い順）
        closed_positions.sort(key=lambda x: x['exit_time'])

        # --- 条件1: 日次累計損失チェック ---
        daily_pnl = sum(p['pnl'] for p in closed_positions)
        if daily_pnl < -CB_DAILY_LOSS_LIMIT_JPY:
            return True, (
                f"日次累計損失 ¥{daily_pnl:,.0f} が上限 -¥{CB_DAILY_LOSS_LIMIT_JPY:,.0f} を超過 "
                f"(24h内 {len(closed_positions)}件)"
            )

        # --- 条件2: 連敗回数チェック ---
        consecutive_losses = 0
        for p in reversed(closed_positions):
            if p['pnl'] < 0:
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= CB_MAX_CONSECUTIVE_LOSSES:
            return True, (
                f"連敗 {consecutive_losses}回 が上限 {CB_MAX_CONSECUTIVE_LOSSES}回 に到達"
            )

        # --- 冷却期間チェック ---
        # 前回トリップ条件を満たした直後の再開を防ぐ
        # (連敗がリセットされても、しばらくはBUYを自粛)
        # → 冷却中かどうかは、最後の負け取引からの経過時間で判定
        if consecutive_losses >= CB_MAX_CONSECUTIVE_LOSSES - 1:
            last_loss_time = closed_positions[-1]['exit_time']
            cooldown_sec = CB_COOLDOWN_HOURS * 3600
            elapsed = now - last_loss_time
            if elapsed < cooldown_sec:
                remaining_min = (cooldown_sec - elapsed) / 60
                return True, (
                    f"冷却期間中 (連敗{consecutive_losses}回後、残り{remaining_min:.0f}分)"
                )

        print(f"Circuit breaker: OK (daily_pnl=¥{daily_pnl:,.0f}, "
              f"consecutive_losses={consecutive_losses})")
        return False, ""

    except Exception as e:
        print(f"Circuit breaker check failed: {e}")
        # チェック失敗時は安全側に倒さない（取引継続）
        return False, ""


def send_notification(name: str, message: str):
    """Slack通知送信"""
    if not SLACK_WEBHOOK_URL:
        print(f"SLACK_WEBHOOK_URL not set, skipping notification: {message}")
        return

    try:
        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": message
                    }
                }
            ]
        }

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=5)
        print(f"Slack notification sent (status: {response.status})")
    except Exception as e:
        print(f"Slack notification failed: {e}")
