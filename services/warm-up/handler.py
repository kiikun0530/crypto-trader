"""
ウォームアップ Lambda
初回デプロイ時に過去データをBinanceから取得してDynamoDBに投入
手動で1回実行する
"""
import json
import os
import urllib.request
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')

# Binance API設定
BINANCE_SYMBOL = 'ETHUSDT'
BINANCE_INTERVAL = '5m'
BINANCE_LIMIT = 1000  # 最大1000件 = 約3.5日分

def handler(event, context):
    """過去データをBinanceから取得してDynamoDB投入"""
    pair = 'eth_usdt'
    
    try:
        # 1. Binance APIから過去データ取得
        print(f"Fetching {BINANCE_LIMIT} candles from Binance...")
        candles = fetch_historical_data()
        print(f"Fetched {len(candles)} candles")
        
        # 2. DynamoDBにバッチ投入
        inserted_count = batch_write_prices(pair, candles)
        
        result = {
            'pair': pair,
            'fetched': len(candles),
            'inserted': inserted_count,
            'oldest_timestamp': candles[0]['timestamp'] if candles else None,
            'newest_timestamp': candles[-1]['timestamp'] if candles else None
        }
        
        print(f"Warm-up complete: {result}")
        
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

def fetch_historical_data() -> list:
    """Binance APIから過去の5分足データを取得"""
    url = f"https://api.binance.com/api/v3/klines?symbol={BINANCE_SYMBOL}&interval={BINANCE_INTERVAL}&limit={BINANCE_LIMIT}"
    req = urllib.request.Request(url)
    
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode())
    
    # [openTime, open, high, low, close, volume, closeTime, ...]
    candles = []
    for candle in data:
        candles.append({
            'timestamp': int(candle[0] / 1000),  # ミリ秒→秒
            'open': float(candle[1]),
            'high': float(candle[2]),
            'low': float(candle[3]),
            'close': float(candle[4]),
            'volume': float(candle[5])
        })
    
    return candles

def batch_write_prices(pair: str, candles: list) -> int:
    """DynamoDBにバッチ投入"""
    table = dynamodb.Table(PRICES_TABLE)
    inserted = 0
    
    # DynamoDBのBatchWriteは最大25件
    batch_size = 25
    for i in range(0, len(candles), batch_size):
        batch = candles[i:i + batch_size]
        
        with table.batch_writer() as writer:
            for candle in batch:
                writer.put_item(Item={
                    'pair': pair,
                    'timestamp': candle['timestamp'],
                    'price': Decimal(str(candle['close'])),
                    'open': Decimal(str(candle['open'])),
                    'high': Decimal(str(candle['high'])),
                    'low': Decimal(str(candle['low'])),
                    'volume': Decimal(str(candle['volume'])),
                    'ttl': candle['timestamp'] + 1209600  # 14日後に削除
                })
                inserted += 1
    
    return inserted
