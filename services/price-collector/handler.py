"""
価格収集 Lambda (マルチタイムフレーム対応)
Step Functions ワークフローの最初のステップとして呼ばれる
指定されたタイムフレームで全対象通貨の価格を取得しDynamoDBに保存
Binance APIから複数通貨ペアを一括取得

タイムフレーム対応:
  15m: 15分足 (Binance interval=15m)
  1h:  1時間足 (Binance interval=1h)
  4h:  4時間足 (Binance interval=4h)
  1d:  日足 (Binance interval=1d)

DynamoDBキー: pair#timeframe (例: "btc_usdt#1h")
TTL: タイムフレームに応じて変動 (14日〜365日)
"""
import json
import os
import time
import urllib.request
import boto3
from decimal import Decimal
import traceback
from botocore.exceptions import ClientError
from trading_common import (
    TRADING_PAIRS, PRICES_TABLE, TIMEFRAME_CONFIG,
    make_pair_tf_key, get_ttl_seconds, dynamodb
)


def handler(event, context):
    """全通貨の価格収集（タイムフレーム指定）"""
    timeframe = event.get('timeframe', '1h')
    pairs = event.get('pairs', list(TRADING_PAIRS.keys()))

    tf_config = TIMEFRAME_CONFIG.get(timeframe)
    if not tf_config:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': f'Unknown timeframe: {timeframe}',
                                'available': list(TIMEFRAME_CONFIG.keys())})
        }

    binance_interval = tf_config['binance_interval']
    ttl_seconds = get_ttl_seconds(timeframe)

    print(f"Price collection: {len(pairs)} pairs, timeframe={timeframe}, "
          f"interval={binance_interval}, ttl={tf_config['ttl_days']}d")

    current_time = int(time.time())
    results = []
    errors = []

    for pair in pairs:
        config = TRADING_PAIRS.get(pair)
        if not config:
            print(f"WARNING: Unknown pair {pair}, skipping")
            continue

        try:
            remaining_time = context.get_remaining_time_in_millis()
            if remaining_time < 30000:
                print(f"WARNING: Low remaining time ({remaining_time}ms) for {pair}")

            # 1. Binance APIから指定TFの最新ローソク足取得
            current_price, candle_time, candle_data = get_candle(
                config['binance'], binance_interval
            )
            print(f"  {config['name']}: ${current_price:,.2f} at {candle_time} ({timeframe})")

            # 2. DynamoDBに保存 (pair#timeframe キー)
            save_price(pair, timeframe, candle_time, current_price, candle_data, ttl_seconds)

            results.append({
                'pair': pair,
                'name': config['name'],
                'price': current_price,
                'timeframe': timeframe,
            })

        except Exception as e:
            error_msg = f"{pair}: {str(e)}"
            print(f"ERROR: {error_msg}")
            print(traceback.format_exc())
            errors.append(error_msg)

    print(f"Price collection completed: {len(results)} OK, {len(errors)} errors")

    return {
        'statusCode': 200,
        'timeframe': timeframe,
        'pairs': pairs,
        'pairs_collected': len(results),
        'errors': len(errors),
    }


def get_candle(binance_symbol: str, interval: str, retries: int = 2) -> tuple:
    """Binance APIから指定インターバルの最新ローソク足を取得"""
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={binance_symbol}&interval={interval}&limit=1")

    current_time = int(time.time())

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (compatible; AWSLambda/1.0)')

            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())

                if not data or not isinstance(data, list) or len(data) == 0:
                    raise ValueError(f"Empty response from Binance for {binance_symbol}")

                candle = data[0]
                if not isinstance(candle, list) or len(candle) < 6:
                    raise ValueError(f"Invalid candle format for {binance_symbol}")

                candle_time = int(int(candle[0]) / 1000)

                # タイムスタンプ妥当性チェック (±7日以内)
                if abs(candle_time - current_time) > 604800:
                    print(f"WARNING: Timestamp seems invalid for {binance_symbol}, using current")
                    candle_time = current_time

                open_price = float(candle[1])
                high_price = float(candle[2])
                low_price = float(candle[3])
                close_price = float(candle[4])
                volume = float(candle[5])

                if close_price <= 0:
                    raise ValueError(f"Invalid close price: {close_price}")
                if high_price < low_price:
                    raise ValueError(f"high ({high_price}) < low ({low_price})")

                candle_data = {
                    'open': open_price,
                    'high': high_price,
                    'low': low_price,
                    'close': close_price,
                    'volume': volume
                }

                return close_price, candle_time, candle_data

        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            if attempt < retries:
                print(f"Attempt {attempt + 1} failed for {binance_symbol}: {e}, retrying...")
                time.sleep(1)
            else:
                raise Exception(f"API failed after {retries + 1} attempts: {e}")
        except Exception as e:
            if attempt < retries:
                print(f"Attempt {attempt + 1} failed for {binance_symbol}: {e}, retrying...")
                time.sleep(1)
            else:
                raise


def save_price(pair: str, timeframe: str, timestamp: int, price: float,
               candle_data: dict, ttl_seconds: int, retries: int = 2):
    """DynamoDBに価格保存（pair#timeframe キー、OHLCV付き）"""
    pair_tf_key = make_pair_tf_key(pair, timeframe)

    for attempt in range(retries + 1):
        try:
            table = dynamodb.Table(PRICES_TABLE)
            item = {
                'pair': pair_tf_key,
                'timestamp': timestamp,
                'price': Decimal(str(round(price, 8))),
                'ttl': timestamp + ttl_seconds,
            }
            if candle_data:
                item['open'] = Decimal(str(round(candle_data['open'], 8)))
                item['high'] = Decimal(str(round(candle_data['high'], 8)))
                item['low'] = Decimal(str(round(candle_data['low'], 8)))
                item['volume'] = Decimal(str(round(candle_data['volume'], 2)))

            table.put_item(Item=item)
            return

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if attempt < retries and error_code in [
                'ProvisionedThroughputExceededException', 'ThrottlingException'
            ]:
                time.sleep(2 ** attempt)
            else:
                raise Exception(f"DynamoDB save failed: {error_code}")
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                raise
