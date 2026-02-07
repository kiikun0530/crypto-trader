"""
Chronos呼び出し Lambda
ECS上のChronos APIを呼び出して価格予測を取得
（ECS未デプロイ時はダミースコアを返す）
"""
import json
import os
import urllib.request
import boto3

dynamodb = boto3.resource('dynamodb')
PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
CHRONOS_API_URL = os.environ.get('CHRONOS_API_URL', '')

def handler(event, context):
    """Chronos予測取得"""
    pair = event.get('pair', 'eth_usdt')
    
    try:
        # 価格履歴取得
        prices = get_price_history(pair, limit=60)
        
        if not prices:
            return {
                'pair': pair,
                'chronos_score': 0.5,
                'prediction': None,
                'reason': 'no_data',
                'current_price': 0
            }
        
        # ECS Chronos APIが設定されている場合
        if CHRONOS_API_URL:
            prediction = call_chronos_api(prices)
            score = prediction_to_score(prediction, prices[-1])
        else:
            # ECS未デプロイ時はテクニカル指標ベースの代替スコア
            score = calculate_momentum_score(prices)
            prediction = None
        
        return {
            'pair': pair,
            'chronos_score': round(score, 3),
            'prediction': prediction,
            'current_price': prices[-1] if prices else 0
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'pair': pair,
            'chronos_score': 0.5,
            'error': str(e),
            'current_price': 0
        }

def get_price_history(pair: str, limit: int = 60) -> list:
    """価格履歴取得"""
    table = dynamodb.Table(PRICES_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=limit
    )
    items = response.get('Items', [])
    return [float(i['price']) for i in reversed(items)]

def call_chronos_api(prices: list) -> float:
    """Chronos API呼び出し"""
    data = json.dumps({'prices': prices}).encode('utf-8')
    req = urllib.request.Request(
        f"{CHRONOS_API_URL}/predict",
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode())
        return result.get('prediction', prices[-1])

def prediction_to_score(prediction: float, current_price: float) -> float:
    """予測価格をスコアに変換 (-1 to 1)"""
    if current_price == 0:
        return 0.5
    
    change_percent = (prediction - current_price) / current_price * 100
    
    # ±3%以上の変動予測で最大スコア
    score = change_percent / 3
    return max(-1, min(1, score))

def calculate_momentum_score(prices: list) -> float:
    """モメンタムベースの代替スコア（ECS未デプロイ時）"""
    if len(prices) < 10:
        return 0.5
    
    # 短期モメンタム（5期間）
    short_momentum = (prices[-1] - prices[-6]) / prices[-6] * 100 if len(prices) >= 6 else 0
    
    # 中期モメンタム（10期間）
    long_momentum = (prices[-1] - prices[-11]) / prices[-11] * 100 if len(prices) >= 11 else 0
    
    # 加重平均
    momentum = short_momentum * 0.6 + long_momentum * 0.4
    
    # スコア変換
    score = momentum / 2  # ±2%で±1
    return max(-1, min(1, score))
