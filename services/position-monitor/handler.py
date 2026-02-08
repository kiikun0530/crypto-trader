"""
ポジション監視 Lambda
5分間隔で全通貨のアクティブポジションを監視し、SL/TP判定
"""
import json
import os
import time
import urllib.request
import boto3

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')
sns = boto3.client('sns')

POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
ORDER_QUEUE_URL = os.environ.get('ORDER_QUEUE_URL', '')
NOTIFICATIONS_TOPIC_ARN = os.environ.get('NOTIFICATIONS_TOPIC_ARN', '')

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

    if NOTIFICATIONS_TOPIC_ARN:
        sns.publish(
            TopicArn=NOTIFICATIONS_TOPIC_ARN,
            Subject=f'{name} {reason_text}',
            Message=message
        )
