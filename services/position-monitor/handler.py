"""
ポジション監視 Lambda
5分間隔で全通貨のアクティブポジションを監視し、SL/TP判定

連続トレーリングストップ:
- ピーク価格をDynamoDBに永続化
- ピークからの下落率でSLを動的に設定
- 利益が大きいほどトレーリング幅を狭める（利益防衛）
- 0-3%: 固定SL (-5%), 3%+: 連続トレーリング開始
"""
import json
import os
import time
import urllib.request
import boto3

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')

POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
ORDER_QUEUE_URL = os.environ.get('ORDER_QUEUE_URL', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

# 通貨ペア設定
DEFAULT_PAIRS = {
    "eth_usdt": {"binance": "ETHUSDT", "coincheck": "eth_jpy", "news": "ETH", "name": "Ethereum"}
}
TRADING_PAIRS = json.loads(os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS)))


def handler(event, context):
    """全通貨のポジション監視"""
    results = []

    # 全通貨ペアのアクティブポジションをチェック
    for pair, config in TRADING_PAIRS.items():
        coincheck_pair = config['coincheck']

        try:
            position = get_active_position(coincheck_pair)

            if not position:
                continue

            # 現在価格取得（Coincheck API）
            current_price = get_current_price(coincheck_pair)

            # SL/TP判定
            entry_price = float(position.get('entry_price', 0))
            stop_loss = float(position.get('stop_loss', entry_price * 0.95))
            take_profit = float(position.get('take_profit', entry_price * 1.10))

            # ⚠️ entry_price 妥当性チェック
            # fill取得バグで entry_price が桁違いに膨張した場合、即SL発動を防止
            if entry_price > 0 and current_price > 0:
                deviation = abs(entry_price - current_price) / current_price
                if deviation > 0.5:  # 50%以上の乖離は異常
                    print(f"⚠️ CRITICAL: {config['name']} entry_price ¥{entry_price:,.0f} "
                          f"deviates {deviation*100:.1f}% from current ¥{current_price:,.0f}. "
                          f"Skipping SL/TP check for this position.")
                    # Slack通知（手動対応を促す）
                    if SLACK_WEBHOOK_URL:
                        try:
                            alert_msg = (
                                f"🚨 {config['name']} entry_price異常\n"
                                f"entry: ¥{entry_price:,.0f}\n"
                                f"current: ¥{current_price:,.0f}\n"
                                f"乖離: {deviation*100:.1f}%\n"
                                f"→ SL/TPチェックをスキップ（手動確認要）"
                            )
                            payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": alert_msg}}]}
                            req = urllib.request.Request(
                                SLACK_WEBHOOK_URL,
                                data=json.dumps(payload).encode('utf-8'),
                                headers={'Content-Type': 'application/json'}
                            )
                            urllib.request.urlopen(req, timeout=5)
                        except Exception:
                            pass
                    continue

            result = {
                'pair': coincheck_pair,
                'name': config['name'],
                'current_price': current_price,
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'action': 'HOLD'
            }

            # 損切り判定
            if current_price <= stop_loss:
                result['action'] = 'STOP_LOSS'
                trigger_sell(coincheck_pair, config['name'], 'stop_loss', current_price, entry_price)

            # 利確判定
            elif current_price >= take_profit:
                result['action'] = 'TAKE_PROFIT'
                trigger_sell(coincheck_pair, config['name'], 'take_profit', current_price, entry_price)

            else:
                # 連続トレーリングストップ: ピーク価格を追跡し、動的SLを算出
                highest_price = float(position.get('highest_price', entry_price))

                # ピーク更新チェック
                if current_price > highest_price:
                    old_peak = highest_price
                    highest_price = current_price
                    update_highest_price(position, highest_price)
                    peak_pnl = (highest_price - entry_price) / entry_price * 100
                    print(f"  🏔️ New peak for {config['name']}: "
                          f"¥{old_peak:,.0f} → ¥{highest_price:,.0f} (+{peak_pnl:.1f}%)")

                # 連続トレーリングストップ計算
                new_sl = calculate_trailing_stop(entry_price, current_price, stop_loss, highest_price)
                if new_sl and new_sl > stop_loss:
                    old_sl = stop_loss
                    stop_loss = new_sl
                    result['stop_loss'] = new_sl
                    # DynamoDBのSLを更新（永続化）
                    update_stop_loss(position, new_sl)
                    pnl_pct = (current_price - entry_price) / entry_price * 100
                    sl_pct = (new_sl - entry_price) / entry_price * 100
                    peak_pct = (highest_price - entry_price) / entry_price * 100
                    print(f"  📈 Trailing stop raised for {config['name']}: "
                          f"SL ¥{old_sl:,.0f} → ¥{new_sl:,.0f} "
                          f"(SL={sl_pct:+.1f}%, peak={peak_pct:+.1f}%, current={pnl_pct:+.1f}%)")
                    # Slack通知
                    notify_trailing_stop(config['name'], coincheck_pair,
                                       old_sl, new_sl, entry_price, current_price, highest_price)

            # P/L計算
            amount = float(position.get('amount', 0))
            unrealized_pnl = (current_price - entry_price) * amount
            result['unrealized_pnl'] = round(unrealized_pnl, 0)
            result['pnl_percent'] = round((current_price - entry_price) / entry_price * 100, 2)

            results.append(result)
            print(f"  {config['name']}: ¥{current_price:,.0f} "
                  f"(P/L: {result['pnl_percent']:+.2f}%) -> {result['action']}")

        except Exception as e:
            print(f"Error monitoring {coincheck_pair}: {e}")

    if not results:
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'No active positions'})
        }

    return {
        'statusCode': 200,
        'body': json.dumps({
            'positions_monitored': len(results),
            'results': results
        })
    }


def get_active_position(pair: str) -> dict:
    """アクティブポジション取得"""
    table = dynamodb.Table(POSITIONS_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        FilterExpression='attribute_not_exists(closed) OR closed = :false',
        ExpressionAttributeValues={
            ':pair': pair,
            ':false': False
        },
        ScanIndexForward=False,
        Limit=1
    )
    items = response.get('Items', [])
    return items[0] if items else None


def get_current_price(pair: str) -> float:
    """Coincheck APIから価格取得"""
    url = f"https://coincheck.com/api/ticker?pair={pair}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        return float(data['last'])


def trigger_sell(pair: str, name: str, reason: str, current_price: float, entry_price: float):
    """売りトリガー発火"""
    timestamp = int(time.time())

    if ORDER_QUEUE_URL:
        sqs.send_message(
            QueueUrl=ORDER_QUEUE_URL,
            MessageBody=json.dumps({
                'pair': pair,
                'signal': 'SELL',
                'score': -1.0,
                'timestamp': timestamp,
                'reason': reason
            })
        )

    pnl_percent = (current_price - entry_price) / entry_price * 100
    emoji = '🔴' if reason == 'stop_loss' else '💰'
    reason_text = '損切り' if reason == 'stop_loss' else '利確'

    message = (
        f"{emoji} {name} {reason_text}トリガー\n"
        f"通貨ペア: {pair}\n"
        f"現在価格: ¥{current_price:,.0f}\n"
        f"参入価格: ¥{entry_price:,.0f}\n"
        f"変動: {pnl_percent:+.2f}%"
    )

    if SLACK_WEBHOOK_URL:
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


def calculate_trailing_stop(entry_price: float, current_price: float,
                            current_sl: float, highest_price: float) -> float:
    """
    連続トレーリングストップ計算
    
    ピーク価格からの下落率で動的にSLを設定。
    利益が大きいほどトレーリング幅を狭める（利益防衛を強化）。
    
    トレーリング幅:
    - 含み益 3-5%:  ピークから2.0%下にSL（広め、まだ成長余地あり）
    - 含み益 5-8%:  ピークから1.5%下にSL（中間）
    - 含み益 8-12%: ピークから1.2%下にSL（狭め、利益防衛優先）
    - 含み益 12%+:  ピークから1.0%下にSL（最狭、大利確保）
    
    Returns: 新しいSL価格 (引き上げ不要ならNone)
    """
    if entry_price <= 0 or highest_price <= 0:
        return None
    
    # ピークからの含み益（%）
    peak_pnl_pct = (highest_price - entry_price) / entry_price * 100
    
    # 含み益3%未満はトレーリング非適用（固定SLのまま）
    if peak_pnl_pct < 3.0:
        return None
    
    # 利益水準に応じたトレーリング幅（%）
    # 利益が大きいほど幅を狭めて利益を守る
    if peak_pnl_pct >= 12.0:
        trail_pct = 1.0   # ピークから1.0%でSL
    elif peak_pnl_pct >= 8.0:
        trail_pct = 1.2   # ピークから1.2%でSL
    elif peak_pnl_pct >= 5.0:
        trail_pct = 1.5   # ピークから1.5%でSL
    else:
        trail_pct = 2.0   # ピークから2.0%でSL（3-5%帯）
    
    # SL = ピーク価格 × (1 - trail幅)
    new_sl = highest_price * (1 - trail_pct / 100)
    
    # 最低でも建値以上を保証（含み益3%以上に到達した場合）
    breakeven = entry_price * 1.001  # わずかに建値の上（手数料分）
    new_sl = max(new_sl, breakeven)
    
    # 現在のSLより高い場合のみ更新（SLは上がるだけ、下がらない）
    if new_sl > current_sl:
        return new_sl
    return None


def update_stop_loss(position: dict, new_sl: float):
    """DynamoDBのstop_lossを更新"""
    from decimal import Decimal
    table = dynamodb.Table(POSITIONS_TABLE)
    try:
        table.update_item(
            Key={
                'pair': position['pair'],
                'position_id': position['position_id']
            },
            UpdateExpression='SET stop_loss = :sl',
            ExpressionAttributeValues={
                ':sl': Decimal(str(round(new_sl, 2)))
            }
        )
    except Exception as e:
        print(f"Failed to update stop_loss in DB: {e}")


def update_highest_price(position: dict, highest_price: float):
    """DynamoDBのhighest_priceを更新（ピーク価格追跡）"""
    from decimal import Decimal
    table = dynamodb.Table(POSITIONS_TABLE)
    try:
        table.update_item(
            Key={
                'pair': position['pair'],
                'position_id': position['position_id']
            },
            UpdateExpression='SET highest_price = :hp',
            ExpressionAttributeValues={
                ':hp': Decimal(str(round(highest_price, 2)))
            }
        )
    except Exception as e:
        print(f"Failed to update highest_price in DB: {e}")


def notify_trailing_stop(name: str, pair: str, old_sl: float, new_sl: float,
                         entry_price: float, current_price: float,
                         highest_price: float = None):
    """トレーリングストップ引き上げのSlack通知"""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        sl_pct = (new_sl - entry_price) / entry_price * 100
        peak_text = ""
        if highest_price and highest_price > 0:
            peak_pct = (highest_price - entry_price) / entry_price * 100
            trail_width = (highest_price - new_sl) / highest_price * 100
            peak_text = f"\nピーク: ¥{highest_price:,.0f} (+{peak_pct:.1f}%), トレール幅: {trail_width:.1f}%"
        message = (
            f"📈 {name} トレーリングストップ引き上げ\n"
            f"通貨: {pair}\n"
            f"SL: ¥{old_sl:,.0f} → ¥{new_sl:,.0f} (entry{sl_pct:+.1f}%)\n"
            f"現在: ¥{current_price:,.0f} (P/L: {pnl_pct:+.1f}%){peak_text}"
        )
        payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]}
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Trailing stop Slack notification failed: {e}")
