"""
価格収集 Lambda
5分間隔で全対象通貨の価格を取得しDynamoDBに保存
Binance APIから複数通貨ペアを一括取得
"""
import json
import os
import time
import urllib.request
import boto3
from decimal import Decimal
import traceback
from botocore.exceptions import ClientError
from trading_common import TRADING_PAIRS, PRICES_TABLE, dynamodb, update_pipeline_status


def handler(event, context):
    """全通貨の価格収集"""
    update_pipeline_status('price_collector', 'running', f'{len(TRADING_PAIRS)}通貨の価格取得中')
    print(f"Starting price collection for {len(TRADING_PAIRS)} trading pairs")
    print(f"Lambda remaining time: {context.get_remaining_time_in_millis()}ms")
    
    current_time = int(time.time())
    results = []
    errors = []

    for pair, config in TRADING_PAIRS.items():
        try:
            print(f"Processing {pair} ({config.get('name', 'Unknown')})...")
            remaining_time = context.get_remaining_time_in_millis()
            if remaining_time < 30000:  # 30秒未満の場合警告
                print(f"WARNING: Low remaining time ({remaining_time}ms) for {pair}")
            
            # 1. Binance APIから現在価格取得（5分足のOHLCV）
            current_price, candle_time, candle_data = get_current_price(config['binance'])
            print(f"Retrieved price: ${current_price:,.2f} at {candle_time}")

            # 2. DynamoDBに価格保存（OHLCV付き）
            save_price(pair, candle_time, current_price, candle_data)
            print(f"Successfully saved price data for {pair}")

            result = {
                'pair': pair,
                'name': config['name'],
                'price': current_price,
            }
            results.append(result)
            print(f"{config['name']} ({pair}): ${current_price:,.2f}")

        except Exception as e:
            print(f"ERROR: {error_msg}")
            print(f"Stack trace for {pair}: {traceback.format_exc()}")
            errors.append(error_msg)
            # エラーが発生しても他の通貨ペアの処理を継続

    print(f"Price collection completed. {len(results)} pairs processed, {len(errors)} errors")
    update_pipeline_status('price_collector', 'completed', f'{len(results)}通貨完了')

    if errors:
        for error in errors:
            print(f"  - {error}")

    final_remaining_time = context.get_remaining_time_in_millis()
    print(f"Lambda execution completed. Remaining time: {final_remaining_time}ms")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'pairs_collected': len(results),
            'errors': len(errors),
            'remaining_time_ms': final_remaining_time
        })
    }


def get_current_price(binance_symbol: str, retries: int = 2) -> tuple:
    """Binance APIから5分足のOHLCVを取得"""
    url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=5m&limit=1"
    print(f"Calling Binance API: {url}")
    
    current_time = int(time.time())
    print(f"Current system time: {current_time}")
    
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (compatible; AWSLambda/1.0)')
            
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status != 200:
                    raise urllib.error.HTTPError(url, response.status, f"HTTP {response.status}", response.headers, None)
                
                raw_data = response.read().decode()
                print(f"API raw response length for {binance_symbol}: {len(raw_data)} chars")
                
                data = json.loads(raw_data)
                print(f"API response received for {binance_symbol}")
                
                # データ検証を強化
                if not data:
                    raise ValueError(f"Empty response data from Binance API for {binance_symbol}")
                
                if not isinstance(data, list):
                    raise ValueError(f"Invalid response format from Binance API for {binance_symbol}: expected list, got {type(data)}")
                
                if len(data) == 0:
                    raise ValueError(f"No candle data returned from Binance API for {binance_symbol}")
                
                candle = data[0]
                if not isinstance(candle, list) or len(candle) < 6:
                    raise ValueError(f"Invalid candle data format for {binance_symbol}: expected list with 6+ elements, got {type(candle)} with {len(candle) if isinstance(candle, list) else 'N/A'} elements")
                
                # Binance kline format: [open_time, open, high, low, close, volume, ...]
                try:
                    open_time_ms = int(candle[0])
                    print(f"Raw timestamp from API for {binance_symbol}: {open_time_ms} ms")
                    
                    candle_time = int(open_time_ms / 1000)  # ミリ秒を秒に変換
                    print(f"Converted timestamp for {binance_symbol}: {candle_time} seconds")
                    
                    # タイムスタンプの妥当性チェック（現在時刻の±1日以内）
                    time_diff = abs(candle_time - current_time)
                    max_allowed_diff = 86400  # 1日（秒）
                    
                    print(f"Time difference for {binance_symbol}: {time_diff} seconds (max allowed: {max_allowed_diff})")
                    
                    if time_diff > max_allowed_diff:
                        print(f"WARNING: Timestamp seems invalid for {binance_symbol}: API={candle_time}, Current={current_time}, Diff={time_diff}s")
                        print(f"Using current time instead for {binance_symbol}")
                        candle_time = current_time
                    
                    open_price = float(candle[1])
                    high_price = float(candle[2])
                    low_price = float(candle[3])
                    close_price = float(candle[4])
                    volume = float(candle[5])
                    
                except (ValueError, TypeError) as e:
                    raise ValueError(f"Failed to parse numeric values from candle data for {binance_symbol}: {str(e)}")
                
                # 価格データ検証を強化
                if close_price <= 0:
                    raise ValueError(f"Invalid close price for {binance_symbol}: {close_price}")
                
                if high_price < low_price:
                    raise ValueError(f"Invalid OHLC data for {binance_symbol}: high ({high_price}) < low ({low_price})")
                
                if not (low_price <= close_price <= high_price):
                    raise ValueError(f"Close price out of range for {binance_symbol}: close={close_price}, high={high_price}, low={low_price}")
                
                candle_data = {
                    'open': open_price,
                    'high': high_price,
                    'low': low_price,
                    'close': close_price,
                    'volume': volume
                }
                
                print(f"Successfully parsed price data for {binance_symbol}: ${close_price:,.2f} (timestamp: {candle_time})")
                return close_price, candle_time, candle_data
                
        except urllib.error.HTTPError as e:
            error_msg = f"HTTP error for {binance_symbol}: {e.code} {e.reason}"
            if attempt < retries:
                print(f"Attempt {attempt + 1}/{retries + 1} failed - {error_msg}, retrying in 1 second...")
                time.sleep(1)
            else:
                print(f"All {retries + 1} attempts failed for {binance_symbol}: {error_msg}")
                raise Exception(f"HTTP error after {retries + 1} attempts: {error_msg}")
                
        except urllib.error.URLError as e:
            error_msg = f"URL error for {binance_symbol}: {str(e)}"
            if attempt < retries:
                print(f"Attempt {attempt + 1}/{retries + 1} failed - {error_msg}, retrying in 1 second...")
                time.sleep(1)
            else:
                print(f"All {retries + 1} attempts failed for {binance_symbol}: {error_msg}")
                raise Exception(f"URL error after {retries + 1} attempts: {error_msg}")
                
        except json.JSONDecodeError as e:
            error_msg = f"JSON decode error for {binance_symbol}: {str(e)}"
            if attempt < retries:
                print(f"Attempt {attempt + 1}/{retries + 1} failed - {error_msg}, retrying in 1 second...")
                time.sleep(1)
            else:
                print(f"All {retries + 1} attempts failed for {binance_symbol}: {error_msg}")
                raise Exception(f"JSON decode error after {retries + 1} attempts: {error_msg}")
                
        except Exception as e:
            error_msg = f"Unexpected error for {binance_symbol}: {str(e)}"
            if attempt < retries:
                print(f"Attempt {attempt + 1}/{retries + 1} failed - {error_msg}, retrying in 1 second...")
                time.sleep(1)
            else:
                print(f"All {retries + 1} attempts failed for {binance_symbol}: {error_msg}")
                raise Exception(f"API call failed after {retries + 1} attempts: {error_msg}")


def save_price(pair: str, timestamp: int, price: float, candle_data: dict = None, retries: int = 2):
    """DynamoDBに価格保存（OHLCV付き）"""
    for attempt in range(retries + 1):
        try:
            table = dynamodb.Table(PRICES_TABLE)
            item = {
                'pair': pair,
                'timestamp': timestamp,
                'price': Decimal(str(round(price, 8))),  # 精度を8桁に制限
                'ttl': timestamp + 1209600  # 14日後に削除
            }
            if candle_data:
                item['open'] = Decimal(str(round(candle_data['open'], 8)))
                item['high'] = Decimal(str(round(candle_data['high'], 8)))
                item['low'] = Decimal(str(round(candle_data['low'], 8)))
                item['volume'] = Decimal(str(round(candle_data['volume'], 2)))
            
            print(f"Saving to DynamoDB table {PRICES_TABLE}: pair={pair}, timestamp={timestamp}, price={price}")
            table.put_item(Item=item)
            print(f"Price data saved to DynamoDB for {pair}")
            return
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            if attempt < retries and error_code in ['ProvisionedThroughputExceededException', 'ThrottlingException']:
                wait_time = 2 ** attempt
                print(f"DynamoDB throttling for {pair} (attempt {attempt + 1}/{retries + 1}): {error_code} - {error_msg}, retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"DynamoDB ClientError for {pair} after {attempt + 1} attempts: {error_code} - {error_msg}")
                raise Exception(f"DynamoDB save failed after {retries + 1} attempts: {error_code} - {error_msg}")
                
        except Exception as e:
            error_msg = f"Unexpected DynamoDB error for {pair}: {str(e)}"
            if attempt < retries:
                print(f"DynamoDB save attempt {attempt + 1}/{retries + 1} failed - {error_msg}, retrying in 1 second...")
                time.sleep(1)
            else:
                print(f"All {retries + 1} DynamoDB save attempts failed for {pair}: {error_msg}")
                raise Exception(f"DynamoDB save failed after {retries + 1} attempts: {error_msg}")
