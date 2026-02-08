"""
注文実行 Lambda
SQSからシグナルを受信し、Coincheck APIで注文実行

マルチ通貨対応:
- pair（eth_jpy, btc_jpy等）から通貨シンボルを動的に判定
- 任意の通貨ペアで買い・売りが可能
- 1ポジション制約（他通貨にポジションがある場合は買わない）
- スコアに応じた投資金額調整（期待値連動）

⚠️ Coincheck成行注文の重要な仕様:
- market_buy / market_sell のレスポンスは amount=None, rate=None
- 約定データは非同期で /api/exchange/orders/transactions から取得
- 約定は複数トランザクションに分割されることがある（limit=100必須）
- 各fundsの値は正負が混在するため abs() で処理する
- 詳細: docs/bugfix-history.md

⚠️ SQSバッチ処理の注意点:
- handler()でraiseすると未処理レコード含むバッチ全体が再配信される
- 注文成功後にDB保存で失敗→raise→再配信→二重注文のリスク
- エラーはログ+Slack通知のみ、raiseしない設計
- _just_bought_pairs: 同一バッチ内のBUY→即SELL防止
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

# スコア閾値と投資比率（期待値連動）
# 現実的なスコア分布: 典型 ±0.25、最大 ±0.55
# アグリゲーターのBUY閾値(基準0.20)を超えたスコアのみ到達する
SCORE_THRESHOLDS = [
    (0.45, 1.00),   # スコア0.45以上 → 利用可能残高の100%（非常に強いシグナル）
    (0.35, 0.75),   # スコア0.35-0.45 → 75%（強いシグナル）
    (0.25, 0.50),   # スコア0.25-0.35 → 50%（中程度のシグナル）
    (0.15, 0.30),   # スコア0.15-0.25 → 30%（弱いシグナル）
]


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
    currency = get_currency_from_pair(pair)
    name = get_currency_name(pair)

    # 1. 現在のポジション確認
    current_position = get_position(pair)

    # 2. 注文判定
    if signal == 'BUY':
        if current_position and current_position.get('side') == 'long':
            print(f"Already have long position for {pair}")
            return

        # 他通貨にポジションがないかチェック（1ポジション制約）
        other_position = check_any_other_position(pair)
        if other_position:
            other_pair = other_position.get('pair', '?')
            print(f"Already have position in {other_pair}, skipping buy for {pair}")
            send_notification(
                name,
                f"⚠️ {name}の買いをスキップ\n"
                f"理由: {other_pair}にポジションあり\n"
                f"スコア: {score:.3f}"
            )
            return

        # 買い注文
        execute_buy(pair, score)

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
        execute_sell(pair, current_position, score)


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


def calculate_order_amount(score: float, available_jpy: float) -> float:
    """
    スコアに応じた投資金額を計算（期待値連動）
    """
    ratio = 0.0
    for threshold, r in SCORE_THRESHOLDS:
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


def execute_buy(pair: str, score: float):
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

        # ポジション保存
        save_position(pair, timestamp, 'long', result, order_amount)

        # 取引履歴保存
        save_trade(pair, timestamp, 'BUY', result)

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


def execute_sell(pair: str, position: dict, score: float):
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

        # 成行売りもamount/rateがNoneで返ることがあるため、約定情報を取得
        sell_rate = result.get('rate')
        sell_amount = result.get('amount')

        # rate が None または無効な場合、約定履歴から取得
        if sell_rate is None or sell_amount is None:
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

        # ポジションクローズ
        close_position(pair, position, timestamp, result)

        # 取引履歴保存
        save_trade(pair, timestamp, 'SELL', result)

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
        send_notification(
            name,
            f"{emoji} {name}売り約定\n"
            f"通貨ペア: {pair}\n"
            f"数量: {amount:.6f} {currency.upper()}\n"
            f"P/L: {pnl_text}{fee_info}\n"
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
    Coincheckの成行買いレスポンスはamount/rateがNoneのため、
    約定後に取引履歴APIで実際の約定量・約定価格を取得する
    
    注意: 約定は複数のトランザクションに分割されることがあるため
    ページネーションのlimit=100で十分なデータを取得する
    """
    if not order_id:
        return None, None

    creds = get_api_credentials()
    if not creds:
        return None, None

    for attempt in range(max_retries):
        time.sleep(2 * (attempt + 1))  # 2秒, 4秒, 6秒待機
        try:
            # 注文のトランザクション（約定履歴）を取得
            # limit=100で十分（1注文で100分割はほぼない）
            result = call_coincheck_api(
                f'/api/exchange/orders/transactions?order_id={order_id}&limit=100',
                'GET', None, creds
            )

            if result and result.get('success') and result.get('transactions'):
                transactions = result['transactions']
                total_amount = sum(abs(float(t.get('funds', {}).get(currency, 0))) for t in transactions)
                total_jpy = sum(abs(float(t.get('funds', {}).get('jpy', 0))) for t in transactions)

                if total_amount > 0:
                    avg_rate = total_jpy / total_amount
                    print(f"Fill data retrieved (attempt {attempt+1}): "
                          f"amount={total_amount}, rate={avg_rate:.2f}, "
                          f"txn_count={len(transactions)}")
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
    """
    if not order_id:
        return None, None

    creds = get_api_credentials()
    if not creds:
        return None, None

    for attempt in range(max_retries):
        time.sleep(2 * (attempt + 1))  # 2秒, 4秒, 6秒待機
        try:
            result = call_coincheck_api(
                f'/api/exchange/orders/transactions?order_id={order_id}',
                'GET', None, creds
            )

            if result and result.get('success') and result.get('transactions'):
                transactions = result['transactions']
                total_amount = sum(abs(float(t.get('funds', {}).get(currency, 0))) for t in transactions)
                total_jpy = sum(abs(float(t.get('funds', {}).get('jpy', 0))) for t in transactions)

                if total_amount > 0 and total_jpy > 0:
                    avg_rate = total_jpy / total_amount
                    print(f"Sell fill data retrieved (attempt {attempt+1}): "
                          f"amount={total_amount}, rate={avg_rate:.2f}, jpy={total_jpy:.0f}")
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


def save_trade(pair: str, timestamp: int, action: str, result: dict):
    """取引履歴保存"""
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

    table.put_item(Item={
        'pair': pair,
        'timestamp': timestamp,
        'action': action,
        'amount': Decimal(str(amount)),
        'rate': Decimal(str(rate)),
        'order_id': str(result.get('id', '')),
        'fee_rate': Decimal(str(TAKER_FEE_RATE))
    })


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
