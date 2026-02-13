"""
ウォームアップ Lambda (マルチタイムフレーム対応)
初回デプロイ時に過去データをBinanceから取得してDynamoDBに投入
手動で1回実行する（全通貨・全タイムフレーム対応）

タイムフレーム別のデータ量:
  15m: 1344本 (14日分) — Binance limit=1344
  1h:  720本  (30日分) — Binance limit=720
  4h:  540本  (90日分) — Binance limit=540
  1d:  250本  (250日分) — Binance limit=250
"""
import json
import os
import urllib.request
import boto3
from decimal import Decimal
from trading_common import (
    TRADING_PAIRS, PRICES_TABLE, TIMEFRAME_CONFIG, ACTIVE_TIMEFRAMES,
    make_pair_tf_key, get_ttl_seconds, dynamodb
)


def handler(event, context):
    """過去データをBinanceから取得してDynamoDB投入（全通貨・全TF）"""

    # 特定通貨・TFのみ指定可能
    target_pair = event.get('pair', None)
    target_tf = event.get('timeframe', None)

    # 通貨フィルタ
    if target_pair:
        pair_config = TRADING_PAIRS.get(target_pair)
        if not pair_config:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': f'Unknown pair: {target_pair}',
                                    'available': list(TRADING_PAIRS.keys())})
            }
        pairs_to_warmup = {target_pair: pair_config}
    else:
        pairs_to_warmup = TRADING_PAIRS

    # TFフィルタ
    if target_tf:
        if target_tf not in TIMEFRAME_CONFIG:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': f'Unknown timeframe: {target_tf}',
                                    'available': ACTIVE_TIMEFRAMES})
            }
        timeframes = [target_tf]
    else:
        timeframes = ACTIVE_TIMEFRAMES

    results = {}

    for pair, config in pairs_to_warmup.items():
        pair_results = {}
        for tf in timeframes:
            try:
                tf_config = TIMEFRAME_CONFIG[tf]
                binance_symbol = config['binance']
                interval = tf_config['binance_interval']
                limit = tf_config['warmup_candles']
                ttl_seconds = get_ttl_seconds(tf)

                print(f"Warming up {config['name']} ({binance_symbol}) "
                      f"TF={tf} limit={limit}...")

                # 1. Binance APIから過去データ取得
                candles = fetch_historical_data(binance_symbol, interval, limit)
                print(f"  Fetched {len(candles)} candles for {tf}")

                # 2. DynamoDBにバッチ投入 (pair#tf キー)
                inserted_count = batch_write_prices(pair, tf, candles, ttl_seconds)

                pair_results[tf] = {
                    'fetched': len(candles),
                    'inserted': inserted_count,
                    'oldest': candles[0]['timestamp'] if candles else None,
                    'newest': candles[-1]['timestamp'] if candles else None
                }
                print(f"  Inserted {inserted_count} records for {pair}#{tf}")

            except Exception as e:
                print(f"Error warming up {pair} TF={tf}: {e}")
                pair_results[tf] = {'error': str(e)}

        results[pair] = {
            'name': config['name'],
            'timeframes': pair_results
        }

    total_pairs = len(results)
    total_tfs = len(timeframes)
    print(f"Warm-up complete: {total_pairs} pairs × {total_tfs} TFs")

    return {
        'statusCode': 200,
        'body': json.dumps(results, default=str)
    }


def fetch_historical_data(binance_symbol: str, interval: str, limit: int) -> list:
    """Binance APIから過去のローソク足データを取得"""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={binance_symbol}&interval={interval}&limit={limit}"
    )
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (compatible; AWSLambda/1.0)')

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


def batch_write_prices(pair: str, timeframe: str, candles: list,
                       ttl_seconds: int) -> int:
    """DynamoDBにバッチ投入（pair#timeframe キー）"""
    table = dynamodb.Table(PRICES_TABLE)
    pair_tf_key = make_pair_tf_key(pair, timeframe)
    inserted = 0

    batch_size = 25
    for i in range(0, len(candles), batch_size):
        batch = candles[i:i + batch_size]

        with table.batch_writer() as writer:
            for candle in batch:
                writer.put_item(Item={
                    'pair': pair_tf_key,
                    'timestamp': candle['timestamp'],
                    'price': Decimal(str(candle['close'])),
                    'open': Decimal(str(candle['open'])),
                    'high': Decimal(str(candle['high'])),
                    'low': Decimal(str(candle['low'])),
                    'volume': Decimal(str(candle['volume'])),
                    'ttl': candle['timestamp'] + ttl_seconds,
                })
                inserted += 1

    return inserted
