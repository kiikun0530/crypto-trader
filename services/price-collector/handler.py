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
    current_time = int(time.time())
    results = []
    triggered_pairs = []
    errors = []

    for pair, config in TRADING_PAIRS.items():
        try:
            print(f"Processing {pair} ({config.get('name', 'Unknown')})...")
            
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

        except Exception as e:
            error_msg = f"Error collecting {pair}: {str(e)}"
            print(error_msg)
            print(f"Stack trace for {pair}: {traceback.format_exc()}")
            errors.append(error_msg)
            # エラーが発生しても他の通貨ペアの処理を継続

    # 6. いずれかの通貨がトリガーされたら、全通貨を一括分析
    analysis_started = False
    if triggered_pairs and STEP_FUNCTION_ARN:
        try:
            all_pairs = list(TRADING_PAIRS.keys())
            start_analysis(all_pairs, current_time, triggered_pairs)
            analysis_started = True
            print("Analysis workflow successfully started")
        except Exception as e:
            error_msg = f"Failed to start analysis workflow: {str(e)}"
            print(error_msg)
            errors.append(error_msg)

    print(f"Collection summary: {len(results)} pairs processed, {len(triggered_pairs)} triggered, analysis: {analysis_started}")
    if errors:
        print(f"Errors encountered: {len(errors)}")
        for error in errors:
            print(f"  - {error}")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'pairs_collected': len(results),
            'triggered': len(triggered_pairs),
            'analysis_started': analysis_started,
            'errors': len(errors)
        })
    }


def get_current_price(binance_symbol: str, retries: int = 2) -> tuple:
    """Binance APIから5分足のOHLCVを取得"""
    url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=5m&limit=1"
    print(f"Calling Binance API: {url}")
    
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                
                # データ検証
                if not data or not isinstance(data, list) or len(data) == 0:
                    raise ValueError(f"Invalid response data from Binance API: {data}")
                
                candle = data[0]
                if len(candle) < 6:
                    raise ValueError(f"Invalid candle data format: {candle}")
                
                # Binance kline format: [open_time, open, high, low, close, volume, ...]
                close_price = float(candle[4])
                candle_time = int(candle[0] / 1000)
                candle_data = {
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': close_price,
                    'volume': float(candle[5])
                }
                
                # 価格データ検証
                if close_price <= 0:
                    raise ValueError(f"Invalid price data: {close_price}")
                
                return close_price, candle_time, candle_data
                
        except Exception as e:
            if attempt < retries:
                print(f"API call attempt {attempt + 1} failed, retrying: {str(e)}")
                time.sleep(1)  # 1秒待機してリトライ
            else:
                print(f"All API call attempts failed for {binance_symbol}")
                raise e


def save_price(pair: str, timestamp: int, price: float, candle_data: dict = None):
    """DynamoDBに価格保存（OHLCV付き）"""
    try:
        table = dynamodb.Table(PRICES_TABLE)
        item = {
            'pair': pair,
            'timestamp': timestamp,
            'price': Decimal(str(price)),
            'ttl': timestamp + 1209600  # 14日後に削除
        }
        if candle_data:
            item['open'] = Decimal(str(candle_data['open']))
            item['high'] = Decimal(str(candle_data['high']))
            item['low'] = Decimal(str(candle_data['low']))
            item['volume'] = Decimal(str(candle_data['volume']))
        
        table.put_item(Item=item)
        
    except Exception as e:
        print(f"Failed to save price data to DynamoDB: {str(e)}")
        raise e


def get_price_at(pair: str, target_time: int) -> float:
    """指定時刻付近の価格取得（5分足のキャンドル境界に対応するため±300秒）"""
    try:
        table = dynamodb.Table(PRICES_TABLE)
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
        if items and 'price' in items[0]:
            return float(items[0]['price'])
        return None
        
    except Exception as e:
        print(f"Failed to get historical price for {pair}: {str(e)}")
        return None


def check_analysis_trigger(pair: str, current_time: int, change_percent: float) -> tuple:
    """分析トリガー判定"""
    try:
        # 急変時は即座に分析
        if change_percent >= VOLATILITY_THRESHOLD:
            return True, 'volatility'

        # 1時間経過で定期分析
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        response = table.get_item(Key={'pair': pair})
        last_analysis = response.get('Item', {}).get('last_analysis_time', 0)

        if current_time - last_analysis >= 3600:
            return True, 'periodic'

        return False, 'skip'
        
    except Exception as e:
        print(f"Failed to check analysis trigger for {pair}: {str(e)}")
        # エラー時はスキップ
        return False, 'error'


def start_analysis(pairs: list, timestamp: int, triggered: list):
    """Step Functions分析ワークフロー開始（全通貨一括）"""
    try:
        # 分析状態を全通貨分更新
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        for pair in pairs:
            table.put_item(Item={
                'pair': pair,
                'last_analysis_time': timestamp
            })

        # Step Functions実行（全通貨リストを渡す）
        reasons = list(set([t['reason'] for t in triggered]))
        sfn.start_execution(
            stateMachineArn=STEP_FUNCTION_ARN,
            input=json.dumps({
                'pairs': pairs,
                'timestamp': timestamp,
                'trigger_reasons': reasons
            })
        )
        triggered_info = ', '.join([f"{t['pair']}({t['reason']})" for t in triggered])
        print(f"Analysis workflow started: {triggered_info} -> analyzing all {len(pairs)} pairs")
        
    except Exception as e:
        print(f"Failed to start analysis workflow: {str(e)}")
        raise e
