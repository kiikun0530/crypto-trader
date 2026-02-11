"""
ウォームアップ Lambda
初回デプロイ時に過去データをBinanceから取得してDynamoDBに投入
手動で1回実行する（全通貨対応）
"""
import json
import os
import urllib.request
import boto3
from decimal import Decimal
from trading_common import TRADING_PAIRS, PRICES_TABLE, dynamodb

BINANCE_INTERVAL = '5m'
BINANCE_LIMIT = 1000  # 最大1000件 = 約3.5日分


def handler(event, context):
    """過去データをBinanceから取得してDynamoDB投入（全通貨）"""

    # 特定通貨のみ指定可能
    target_pair = event.get('pair', None)

    if target_pair:
        pair_config = TRADING_PAIRS.get(target_pair)
        if not pair_config:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': f'Unknown pair: {target_pair}', 'available': list(TRADING_PAIRS.keys())})
            }
        pairs_to_warmup = {target_pair: pair_config}
    else:
        pairs_to_warmup = TRADING_PAIRS

    results = {}

    for pair, config in pairs_to_warmup.items():
        try:
            binance_symbol = config['binance']
            print(f"Warming up {config['name']} ({binance_symbol})...")

            # 1. Binance APIから過去データ取得
            candles = fetch_historical_data(binance_symbol)
            print(f"  Fetched {len(candles)} candles")

            # 2. DynamoDBにバッチ投入
            inserted_count = batch_write_prices(pair, candles)

            results[pair] = {
                'name': config['name'],
                'fetched': len(candles),
                'inserted': inserted_count,
                'oldest': candles[0]['timestamp'] if candles else None,
                'newest': candles[-1]['timestamp'] if candles else None
            }
            print(f"  Inserted {inserted_count} records")

        except Exception as e:
            print(f"Error warming up {pair}: {e}")
            results[pair] = {'error': str(e)}

    print(f"Warm-up complete: {len(results)} pairs processed")

    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }


def fetch_historical_data(binance_symbol: str) -> list:
    """Binance APIから過去の5分足データを取得"""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={binance_symbol}&interval={BINANCE_INTERVAL}&limit={BINANCE_LIMIT}"
    )
    req = urllib.request.Request(url)

    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode())

    candles = []
    for candle in data:
        candles.append({
            'timestamp': int(candle[0] / 1000),
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
