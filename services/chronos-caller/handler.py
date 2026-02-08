"""
Chronos呼び出し Lambda
SageMaker Serverless Endpoint 上の Chronos-T5-Tiny を呼び出して価格予測を取得
（SageMaker 未デプロイ時はモメンタムベースの代替スコアを使用）
"""
import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
sagemaker_runtime = boto3.client('sagemaker-runtime')

PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
CHRONOS_ENDPOINT_NAME = os.environ.get('CHRONOS_ENDPOINT_NAME', '')
PREDICTION_LENGTH = int(os.environ.get('PREDICTION_LENGTH', '12'))


def handler(event, context):
    """Chronos予測取得"""
    pair = event.get('pair', 'eth_usdt')

    try:
        # 価格履歴取得
        prices = get_price_history(pair, limit=60)

        if not prices:
            return {
                'pair': pair,
                'chronos_score': 0.0,
                'prediction': None,
                'reason': 'no_data',
                'current_price': 0
            }

        # SageMaker Endpoint が設定されている場合
        if CHRONOS_ENDPOINT_NAME:
            result = call_sagemaker_endpoint(prices)
            predictions = result.get('predictions', [])

            if predictions and not result.get('error'):
                score = predictions_to_score(predictions, prices[-1])
            else:
                print(f"SageMaker returned error or empty: {result}")
                score = calculate_momentum_score(prices)
                predictions = None
        else:
            # SageMaker未デプロイ時はモメンタムベースの代替スコア
            score = calculate_momentum_score(prices)
            predictions = None

        return {
            'pair': pair,
            'chronos_score': round(score, 3),
            'prediction': predictions,
            'current_price': prices[-1] if prices else 0
        }

    except Exception as e:
        print(f"Error calling Chronos: {str(e)}")
        import traceback
        traceback.print_exc()

        # フォールバック: モメンタム
        try:
            prices = get_price_history(pair, limit=60)
            score = calculate_momentum_score(prices) if prices else 0.0
        except:
            score = 0.0

        return {
            'pair': pair,
            'chronos_score': round(score, 3),
            'prediction': None,
            'error': str(e),
            'current_price': prices[-1] if prices else 0
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


def call_sagemaker_endpoint(prices: list) -> dict:
    """SageMaker Serverless Endpoint 呼び出し"""
    payload = json.dumps({
        'prices': prices,
        'prediction_length': PREDICTION_LENGTH
    })

    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=CHRONOS_ENDPOINT_NAME,
        ContentType='application/json',
        Body=payload
    )

    result = json.loads(response['Body'].read().decode())
    return result


def predictions_to_score(predictions: list, current_price: float) -> float:
    """
    予測価格列をスコアに変換 (-1 to +1)

    ロジック:
    - 予測系列の加重平均を計算（直近予測を軽く、遠い予測を重く）
    - 現在価格との変化率を計算
    - ±3% の変動で ±1.0 にスケール
    """
    if not predictions or current_price <= 0:
        return 0.0

    # 遠い将来ほど重みを大きく（トレンド方向を重視）
    n = len(predictions)
    weights = [(i + 1) / n for i in range(n)]
    total_weight = sum(weights)

    weighted_avg = sum(p * w for p, w in zip(predictions, weights)) / total_weight

    change_percent = (weighted_avg - current_price) / current_price * 100

    # ±3% で ±1.0 にスケール
    score = change_percent / 3.0
    return max(-1.0, min(1.0, score))


def calculate_momentum_score(prices: list) -> float:
    """モメンタムベースの代替スコア（SageMaker未デプロイ時のフォールバック）"""
    if len(prices) < 10:
        return 0.0

    # 短期モメンタム（5期間）
    short_momentum = (prices[-1] - prices[-6]) / prices[-6] * 100 if len(prices) >= 6 else 0

    # 中期モメンタム（10期間）
    long_momentum = (prices[-1] - prices[-11]) / prices[-11] * 100 if len(prices) >= 11 else 0

    # 加重平均
    momentum = short_momentum * 0.6 + long_momentum * 0.4

    # スコア変換（±2% で ±1.0）
    score = momentum / 2
    return max(-1.0, min(1.0, score))
