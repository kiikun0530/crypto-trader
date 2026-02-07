"""
価格収集 + 変動検知 Lambda
5分間隔で価格を取得し、変動を検知して分析をトリガー
Binance APIからETH/USDTを取得
"""
import json
import os
import time
import urllib.request
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
sfn = boto3.client('stepfunctions')

PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
ANALYSIS_STATE_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')
VOLATILITY_THRESHOLD = float(os.environ.get('VOLATILITY_THRESHOLD', '0.3'))
STEP_FUNCTION_ARN = os.environ.get('STEP_FUNCTION_ARN', '')

# Binance API設定
BINANCE_SYMBOL = 'ETHUSDT'
BINANCE_INTERVAL = '5m'

def handler(event, context):
    """価格収集 + 変動検知"""
    pair = 'eth_usdt'  # DynamoDB用のpair名
    
    try:
        # 1. Binance APIから現在価格取得（5分足の終値）
        current_price, candle_time = get_current_price()
        current_time = int(time.time())
        
        # 2. DynamoDBに価格保存（足の開始時刻をキーに）
        save_price(pair, candle_time, current_price)
        
        # 3. 1時間前の価格取得（5分足×12本分）
        price_1h_ago = get_price_at(pair, current_time - 3600)
        
        # 4. 変動率計算
        if price_1h_ago:
            change_percent = abs(current_price - price_1h_ago) / price_1h_ago * 100
        else:
            change_percent = 0
        
        # 5. 分析トリガー判定
        should_analyze, reason = check_analysis_trigger(pair, current_time, change_percent)
        
        result = {
            'pair': pair,
            'price': current_price,
            'timestamp': candle_time,
            'change_percent': round(change_percent, 3),
            'should_analyze': should_analyze,
            'reason': reason
        }
        
        # 6. 分析トリガー発火
        if should_analyze and STEP_FUNCTION_ARN:
            start_analysis(pair, candle_time, reason)
            result['analysis_triggered'] = True
        
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

def get_current_price() -> tuple:
    """Binance APIから5分足の終値を取得"""
    url = f"https://api.binance.com/api/v3/klines?symbol={BINANCE_SYMBOL}&interval={BINANCE_INTERVAL}&limit=1"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        # [openTime, open, high, low, close, volume, ...]
        candle = data[0]
        close_price = float(candle[4])
        candle_time = int(candle[0] / 1000)  # ミリ秒→秒
        return close_price, candle_time

def save_price(pair: str, timestamp: int, price: float):
    """DynamoDBに価格保存"""
    table = dynamodb.Table(PRICES_TABLE)
    table.put_item(Item={
        'pair': pair,
        'timestamp': timestamp,
        'price': Decimal(str(price)),
        'ttl': timestamp + 1209600  # 14日後に削除
    })

def get_price_at(pair: str, target_time: int) -> float:
    """指定時刻付近の価格取得"""
    table = dynamodb.Table(PRICES_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair AND #ts BETWEEN :start AND :end',
        ExpressionAttributeNames={'#ts': 'timestamp'},
        ExpressionAttributeValues={
            ':pair': pair,
            ':start': target_time - 60,
            ':end': target_time + 60
        },
        Limit=1
    )
    items = response.get('Items', [])
    if items:
        return float(items[0]['price'])
    return None

def check_analysis_trigger(pair: str, current_time: int, change_percent: float) -> tuple:
    """分析トリガー判定"""
    # 急変時（VOLATILITY_THRESHOLD以上）→ 即時分析
    if change_percent >= VOLATILITY_THRESHOLD:
        return True, 'volatility'
    
    # 1時間経過 → 定期分析
    table = dynamodb.Table(ANALYSIS_STATE_TABLE)
    response = table.get_item(Key={'pair': pair})
    last_analysis = response.get('Item', {}).get('last_analysis_time', 0)
    
    if current_time - last_analysis >= 3600:
        return True, 'periodic'
    
    return False, 'skip'

def start_analysis(pair: str, timestamp: int, reason: str):
    """Step Functions分析ワークフロー開始"""
    # 分析状態更新
    table = dynamodb.Table(ANALYSIS_STATE_TABLE)
    table.put_item(Item={
        'pair': pair,
        'last_analysis_time': timestamp
    })
    
    # Step Functions実行
    sfn.start_execution(
        stateMachineArn=STEP_FUNCTION_ARN,
        input=json.dumps({
            'pair': pair,
            'timestamp': timestamp,
            'trigger_reason': reason
        })
    )
