"""
価格収集 + 変動検知 Lambda
5分間隔で全対象通貨の価格を取得し、変動を検知して分析をトリガー
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

dynamodb = boto3.resource('dynamodb')
sfn = boto3.client('stepfunctions')

PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
ANALYSIS_STATE_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')
VOLATILITY_THRESHOLD = float(os.environ.get('VOLATILITY_THRESHOLD', '0.3'))
STEP_FUNCTION_ARN = os.environ.get('STEP_FUNCTION_ARN', '')

# 通貨ペア設定（環境変数からJSON読み込み）
DEFAULT_PAIRS = {
    "eth_usdt": {"binance": "ETHUSDT", "coincheck": "eth_jpy", "news": "ETH", "name": "Ethereum"}
}
TRADING_PAIRS = json.loads(os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS)))


def handler(event, context):
    """全通貨の価格収集 + 変動検知"""
    print(f"Starting price collection for {len(TRADING_PAIRS)} trading pairs")
    print(f"Lambda remaining time: {context.get_remaining_time_in_millis()}ms")
    
    current_time = int(time.time())
    results = []
    triggered_pairs = []
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
            print(f"Saved to DynamoDB: {pair}")

            # 3. 1時間前の価格取得
            price_1h_ago = get_price_at(pair, current_time - 3600)
            print(f"Price 1h ago: ${price_1h_ago:.2f}" if price_1h_ago else "Price 1h ago: Not available")

            # 4. 変動率計算
            if price_1h_ago:
                change_percent = abs(current_price - price_1h_ago) / price_1h_ago * 100
            else:
                change_percent = 0
            print(f"Change percent: {change_percent:.3f}%")

            # 5. 分析トリガー判定
            should_analyze, reason = check_analysis_trigger(pair, current_time, change_percent)
            print(f"Analysis trigger: {should_analyze} ({reason})")

            result = {
                'pair': pair,
                'name': config['name'],
                'price': current_price,
                'change_percent': round(change_percent, 3),
                'should_analyze': should_analyze,
                'reason': reason
            }
            results.append(result)
            print(f"{config['name']} ({pair}): ${current_price:,.2f} ({change_percent:+.2f}%) -> {reason}")

            if should_analyze:
                triggered_pairs.append({'pair': pair, 'reason': reason})
                print(f"Added to triggered pairs: {pair} (reason: {reason})")

        except Exception as e:
            error_msg = f"Error collecting {pair}: {str(e)}"
            print(f"ERROR: {error_msg}")
            print(f"Stack trace for {pair}: {traceback.format_exc()}")
            errors.append(error_msg)
            # エラーが発生しても他の通貨ペアの処理を継続

    print(f"Price collection completed. Triggered pairs: {len(triggered_pairs)}")
    for tp in triggered_pairs:
        print(f"  - {tp['pair']}: {tp['reason']}")

    # 6. いずれかの通貨がトリガーされたら、全通貨を一括分析
    analysis_started = False
    if triggered_pairs and STEP_FUNCTION_ARN:
        try:
            print(f"Starting analysis workflow for {len(triggered_pairs)} triggered pairs")
            all_pairs = list(TRADING_PAIRS.keys())
            start_analysis(all_pairs, current_time, triggered_pairs)
            analysis_started = True
            print("Analysis workflow successfully started")
        except Exception as e:
            error_msg = f"Failed to start analysis workflow: {str(e)}"
            print(f"ERROR: {error_msg}")
            print(f"Analysis workflow error trace: {traceback.format_exc()}")
            errors.append(error_msg)
    elif triggered_pairs and not STEP_FUNCTION_ARN:
        print("WARNING: Triggered pairs found but STEP_FUNCTION_ARN is not configured")
    else:
        print("No triggered pairs, skipping analysis workflow")

    print(f"Collection summary: {len(results)} pairs processed, {len(triggered_pairs)} triggered, analysis: {analysis_started}")
    if errors:
        print(f"Errors encountered: {len(errors)}")
        for error in errors:
            print(f"  - {error}")

    final_remaining_time = context.get_remaining_time_in_millis()
    print(f"Lambda execution completed. Remaining time: {final_remaining_time}ms")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'pairs_collected': len(results),
            'triggered': len(triggered_pairs),
            'analysis_started': analysis_started,
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


def get_price_at(pair: str, target_time: int) -> float:
    """指定時刻付近の価格取得（5分足のキャンドル境界に対応するため±300秒）"""
    try:
        table = dynamodb.Table(PRICES_TABLE)
        print(f"Querying historical price for {pair} around timestamp {target_time}")
        
        response = table.query(
            KeyConditionExpression='pair = :pair AND #ts BETWEEN :start AND :end',
            ExpressionAttributeNames={'#ts': 'timestamp'},
            ExpressionAttributeValues={
                ':pair': pair,
                ':start': target_time - 300,
                ':end': target_time + 300
            },
            ScanIndexForward=False,
            Limit=1
        )
        
        items = response.get('Items', [])
        print(f"Historical price query returned {len(items)} items for {pair}")
        
        if items and 'price' in items[0]:
            historical_price = float(items[0]['price'])
            found_timestamp = items[0].get('timestamp', 'unknown')
            print(f"Found historical price for {pair}: ${historical_price:.2f} at timestamp {found_timestamp}")
            return historical_price
        else:
            print(f"No historical price found for {pair} around timestamp {target_time}")
            return None
            
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"DynamoDB ClientError getting historical price for {pair}: {error_code} - {error_msg}")
        return None
    except Exception as e:
        print(f"Failed to get historical price for {pair}: {str(e)}")
        print(f"Historical price query error trace for {pair}: {traceback.format_exc()}")
        return None


def check_analysis_trigger(pair: str, current_time: int, change_percent: float) -> tuple:
    """分析トリガー判定"""
    try:
        # 急変時は即座に分析
        if change_percent >= VOLATILITY_THRESHOLD:
            print(f"Volatility trigger activated for {pair}: {change_percent:.3f}% >= {VOLATILITY_THRESHOLD}%")
            return True, 'volatility'

        # 1時間経過で定期分析
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        print(f"Checking last analysis time for {pair}")
        
        response = table.get_item(Key={'pair': pair})
        last_analysis = response.get('Item', {}).get('last_analysis_time', 0)
        
        time_since_last = current_time - last_analysis
        print(f"Time since last analysis for {pair}: {time_since_last}s (last: {last_analysis}, current: {current_time})")

        if time_since_last >= 3600:
            print(f"Periodic trigger activated for {pair}: {time_since_last}s >= 3600s")
            return True, 'periodic'

        print(f"No trigger for {pair}: change={change_percent:.3f}% < {VOLATILITY_THRESHOLD}%, time={time_since_last}s < 3600s")
        return False, 'skip'
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"DynamoDB ClientError checking analysis trigger for {pair}: {error_code} - {error_msg}")
        return False, 'error'
    except Exception as e:
        print(f"Failed to check analysis trigger for {pair}: {str(e)}")
        print(f"Analysis trigger check error trace for {pair}: {traceback.format_exc()}")
        return False, 'error'


def start_analysis(pairs: list, timestamp: int, triggered: list):
    """Step Functions分析ワークフロー開始（全通貨一括）"""
    try:
        print(f"Updating analysis state for {len(pairs)} pairs...")
        # 分析状態を全通貨分更新
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        successful_updates = 0
        
        for pair in pairs:
            try:
                table.put_item(Item={
                    'pair': pair,
                    'last_analysis_time': timestamp
                })
                print(f"Updated analysis state for {pair}")
                successful_updates += 1
            except Exception as e:
                print(f"Failed to update analysis state for {pair}: {str(e)}")
                # 個別エラーは警告として扱い、継続

        print(f"Successfully updated analysis state for {successful_updates}/{len(pairs)} pairs")

        # Step Functions実行（全通貨リストを渡す）
        reasons = list(set([t['reason'] for t in triggered]))
        execution_input = {
            'pairs': pairs,
            'timestamp': timestamp,
            'trigger_reasons': reasons
        }
        
        print(f"Starting Step Functions execution with input: {json.dumps(execution_input)}")
        response = sfn.start_execution(
            stateMachineArn=STEP_FUNCTION_ARN,
            input=json.dumps(execution_input)
        )
        
        execution_arn = response.get('executionArn', 'unknown')
        triggered_info = ', '.join([f"{t['pair']}({t['reason']})" for t in triggered])
        print(f"Analysis workflow started: {triggered_info} -> analyzing all {len(pairs)} pairs")
        print(f"Step Functions execution ARN: {execution_arn}")
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"Step Functions ClientError: {error_code} - {error_msg}")
        raise Exception(f"Step Functions execution failed: {error_code} - {error_msg}")
    except Exception as e:
        print(f"Failed to start analysis workflow: {str(e)}")
        print(f"Start analysis error trace: {traceback.format_exc()}")
        raise e
