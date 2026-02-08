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
    current_time = int(time.time())
    results = []
    triggered_pairs = []

    for pair, config in TRADING_PAIRS.items():
        try:
            # 1. Binance APIから現在価格取得（5分足の終値）
            current_price, candle_time = get_current_price(config['binance'])

            # 2. DynamoDBに価格保存
            save_price(pair, candle_time, current_price)

            # 3. 1時間前の価格取得
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
                'name': config['name'],
                'price': current_price,
                'change_percent': round(change_percent, 3),
                'should_analyze': should_analyze,
                'reason': reason
            }
            results.append(result)
            print(f"  {config['name']} ({pair}): ${current_price:,.2f} ({change_percent:+.2f}%) -> {reason}")

            if should_analyze:
                triggered_pairs.append({'pair': pair, 'reason': reason})

        except Exception as e:
            print(f"Error collecting {pair}: {e}")

    # 6. いずれかの通貨がトリガーされたら、全通貨を一括分析
    analysis_started = False
    if triggered_pairs and STEP_FUNCTION_ARN:
        all_pairs = list(TRADING_PAIRS.keys())
        start_analysis(all_pairs, current_time, triggered_pairs)
        analysis_started = True

    print(f"Collected {len(results)} pairs, {len(triggered_pairs)} triggered, analysis: {analysis_started}")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'pairs_collected': len(results),
            'triggered': len(triggered_pairs),
            'analysis_started': analysis_started
        })
    }


def get_current_price(binance_symbol: str) -> tuple:
    """Binance APIから5分足の終値を取得"""
    url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval=5m&limit=1"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        candle = data[0]
        close_price = float(candle[4])
        candle_time = int(candle[0] / 1000)
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
    """指定時刻付近の価格取得（5分足のキャンドル境界に対応するため±300秒）"""
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
    if items:
        return float(items[0]['price'])
    return None


def check_analysis_trigger(pair: str, current_time: int, change_percent: float) -> tuple:
    """分析トリガー判定"""
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


def start_analysis(pairs: list, timestamp: int, triggered: list):
    """Step Functions分析ワークフロー開始（全通貨一括）"""
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
