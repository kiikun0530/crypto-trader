"""
ポジション監視 Lambda
1分間隔でポジションを監視し、SL/TP判定
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

def handler(event, context):
    """ポジション監視"""
    pair = 'eth_jpy'
    
    try:
        # 1. アクティブポジション取得
        position = get_active_position(pair)
        
        if not position:
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'No active position'})
            }
        
        # 2. 現在価格取得
        current_price = get_current_price(pair)
        
        # 3. SL/TP判定
        entry_price = float(position.get('entry_price', 0))
        stop_loss = float(position.get('stop_loss', entry_price * 0.95))
        take_profit = float(position.get('take_profit', entry_price * 1.10))
        
        result = {
            'pair': pair,
            'current_price': current_price,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'action': 'HOLD'
        }
        
        # 損切り判定
        if current_price <= stop_loss:
            result['action'] = 'STOP_LOSS'
            trigger_sell(pair, 'stop_loss', current_price, entry_price)
        
        # 利確判定
        elif current_price >= take_profit:
            result['action'] = 'TAKE_PROFIT'
            trigger_sell(pair, 'take_profit', current_price, entry_price)
        
        # P/L計算
        amount = float(position.get('amount', 0))
        unrealized_pnl = (current_price - entry_price) * amount
        result['unrealized_pnl'] = round(unrealized_pnl, 0)
        result['pnl_percent'] = round((current_price - entry_price) / entry_price * 100, 2)
        
        return {
            'statusCode': 200,
            'body': json.dumps(result)
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
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

def trigger_sell(pair: str, reason: str, current_price: float, entry_price: float):
    """売りトリガー発火"""
    timestamp = int(time.time())
    
    # SQSに売りメッセージ送信
    if ORDER_QUEUE_URL:
        sqs.send_message(
            QueueUrl=ORDER_QUEUE_URL,
            MessageBody=json.dumps({
                'pair': pair,
                'signal': 'SELL',
                'score': -1.0,  # 強制売り
                'timestamp': timestamp,
                'reason': reason
            })
        )
    
    # 通知
    pnl_percent = (current_price - entry_price) / entry_price * 100
    emoji = '🔴' if reason == 'stop_loss' else '💰'
    reason_text = '損切り' if reason == 'stop_loss' else '利確'
    
    message = f"{emoji} {reason_text}トリガー\n現在価格: ¥{current_price:,.0f}\n参入価格: ¥{entry_price:,.0f}\n変動: {pnl_percent:+.2f}%"
    
    if NOTIFICATIONS_TOPIC_ARN:
        sns.publish(
            TopicArn=NOTIFICATIONS_TOPIC_ARN,
            Subject=f'ETH {reason_text}',
            Message=message
        )
