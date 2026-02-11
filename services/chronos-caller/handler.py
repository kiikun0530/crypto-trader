"""
Chronos呼び出し Lambda (SageMaker Endpoint版)
SageMaker Serverless Endpoint 上の Chronos-2 (120M) を呼び出し
確信度 (confidence) 付きのスコアを返す

モデル遷移履歴:
  v1: ONNX Tiny(8M)  — 軽量低精度
  v2: T5-Base(200M)  — 50回サンプリング、高精度低速
  v3: Chronos-2(120M) — 分位数直接出力、250倍高速、10%高精度

改善点 (vs v2 T5-Base):
- モデル: T5-Base(200M) → Chronos-2(120M) — 軽量化+高精度
- 推論: 50回サンプリング → 分位数直接出力 — 250倍高速
- 出力: q10/q50/q90 の分位数予測を追加
- スコアリング: ±3%スケール + 分位数ベースの確信度
"""
import json
import os
import time
import random
import boto3
import traceback
from botocore.exceptions import ClientError
from botocore.config import Config

# SageMaker用のリトライ設定を修正（無効なパラメータを削除）
sagemaker_config = Config(
    retries={
        'max_attempts': 3,
        'mode': 'adaptive'
    },
    read_timeout=60,
    connect_timeout=10
)

dynamodb = boto3.resource('dynamodb')
sagemaker_runtime = boto3.client('sagemaker-runtime', config=sagemaker_config)
sagemaker_client = boto3.client('sagemaker', config=sagemaker_config)

PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
SAGEMAKER_ENDPOINT = os.environ.get('SAGEMAKER_ENDPOINT', 'eth-trading-chronos-base')
ANALYSIS_STATE_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')
PREDICTION_LENGTH = int(os.environ.get('PREDICTION_LENGTH', '12'))
INPUT_LENGTH = int(os.environ.get('INPUT_LENGTH', '336'))  # 336 × 5min = 28h (日次サイクル1周+α)

# スコアリング設定
SCORE_SCALE_PERCENT = 1.5    # ±1.5%変動で±1.0 (旧: ±3%では5分足12本先で常にゼロ付近だった)

# リトライ設定 (SageMaker Serverlessは冷起動に時間がかかるため長めに設定)
MAX_RETRIES = 5
BASE_DELAY = 3.0   # 基本待機時間（秒）- SageMaker Serverlessの冷起動考慮
MAX_DELAY = 45.0   # 最大待機時間（秒）- 十分な回復時間を確保


def _update_pipeline(stage, status, detail=''):
    try:
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        now = int(time.time())
        table.update_item(
            Key={'pair': 'pipeline_status'},
            UpdateExpression='SET #s = :info, updated_at = :ts',
            ExpressionAttributeNames={'#s': stage},
            ExpressionAttributeValues={
                ':info': {'status': status, 'timestamp': now, 'detail': detail},
                ':ts': now,
            },
        )
    except Exception:
        pass


def handler(event, context):
    """Chronos SageMaker予測取得"""
    pair = event.get('pair', 'eth_usdt')
    _update_pipeline('chronos', 'running', f'{pair} AI価格予測中')

    try:
        prices = get_price_history(pair, limit=INPUT_LENGTH)

        if not prices or len(prices) < 30:
            return {
                'pair': pair,
                'chronos_score': 0.0,
                'confidence': 0.0,
                'prediction': None,
                'reason': 'insufficient_data',
                'data_points': len(prices) if prices else 0,
                'current_price': 0
            }

        # エンドポイント存在確認
        endpoint_status = check_endpoint_availability()
        if endpoint_status in ['not_found', 'permission_error']:
            print(f"[INFO] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' unavailable ({endpoint_status}), using fallback")
            return _fallback_response(pair, prices, endpoint_status)
        elif endpoint_status == 'not_ready':
            print(f"[INFO] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' not ready, using fallback")
            return _fallback_response(pair, prices, 'endpoint_not_ready')

        # SageMaker Endpoint で推論（リトライ機能付き）
        try:
            result = invoke_sagemaker_with_retry(prices, PREDICTION_LENGTH)
            if result and 'median' in result:
                score = predictions_to_score(result, prices[-1])
                confidence = result.get('confidence', 0.5)

                print(f"SageMaker inference OK: {pair}, score={score:.3f}, "
                      f"confidence={confidence:.3f}, data_points={len(prices)}")

                _update_pipeline('chronos', 'completed', f'{pair} score={score:.3f}')
                return {
                    'pair': pair,
                    'chronos_score': round(score, 3),
                    'confidence': round(confidence, 3),
                    'prediction': result.get('median'),
                    'prediction_std': result.get('std'),
                    'prediction_q10': result.get('q10'),
                    'prediction_q90': result.get('q90'),
                    'current_price': prices[-1],
                    'data_points': len(prices),
                    'model': result.get('model', 'chronos-2'),
                }
            else:
                print(f"SageMaker returned empty, fallback to momentum: {pair}")
                return _fallback_response(pair, prices, 'empty_response')

        except Exception as e:
            print(f"SageMaker inference failed: {e}, fallback to momentum: {pair}")
            return _fallback_response(pair, prices, str(e))

    except Exception as e:
        print(f"Error in chronos-caller: {str(e)}")
        traceback.print_exc()
        return _fallback_response(pair, [], str(e))


def check_endpoint_availability():
    """
    SageMakerエンドポイントの存在と状態を確認
    
    Returns:
        'ready': エンドポイントが存在し利用可能
        'not_ready': エンドポイントは存在するが利用不可
        'not_found': エンドポイントが存在しない
        'permission_error': 権限不足
    """
    try:
        response = sagemaker_client.describe_endpoint(EndpointName=SAGEMAKER_ENDPOINT)
        status = response.get('EndpointStatus', 'Unknown')
        
        if status == 'InService':
            print(f"[DEBUG] Endpoint status check result: ready")
            return 'ready'
        else:
            print(f"[WARN] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' status: {status}")
            return 'not_ready'
            
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        
        if error_code == 'ValidationException' or 'not found' in str(e).lower():
            print(f"[INFO] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' not found")
            return 'not_found'
        elif error_code == 'AccessDeniedException':
            print(f"[WARN] Access denied when checking endpoint '{SAGEMAKER_ENDPOINT}': {str(e)}")
            return 'permission_error'
        else:
            print(f"[WARN] Failed to check endpoint '{SAGEMAKER_ENDPOINT}': {error_code}")
            return 'permission_error'
            
    except Exception as e:
        print(f"[ERROR] Unexpected error checking endpoint '{SAGEMAKER_ENDPOINT}': {str(e)}")
        return 'permission_error'


def _fallback_response(pair: str, prices: list, reason: str) -> dict:
    """フォールバック: モメンタムベーススコア"""
    try:
        if not prices:
            prices = get_price_history(pair, limit=60)
        score = calculate_momentum_score(prices) if prices else 0.0
    except Exception:
        score = 0.0

    return {
        'pair': pair,
        'chronos_score': round(score, 3),
        'confidence': 0.1,  # フォールバック時は低確信度
        'prediction': None,
        'current_price': prices[-1] if prices else 0,
        'data_points': len(prices) if prices else 0,
        'model': 'momentum_fallback',
        'reason': reason
    }


def get_price_history(pair: str, limit: int = 200) -> list:
    """
    価格履歴取得

    OHLC データがある場合は Typical Price = (High + Low + Close) / 3 を返す。
    ローソク足の値動きの重心を使うことで、close のみより豊かな情報を Chronos に提供。
    OHLC がない古いレコードは従来通り close にフォールバック。
    """
    table = dynamodb.Table(PRICES_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=limit
    )
    items = response.get('Items', [])

    prices = []
    for item in reversed(items):
        high = item.get('high')
        low = item.get('low')
        close = float(item['price'])

        if high is not None and low is not None:
            typical = (float(high) + float(low) + close) / 3
            prices.append(typical)
        else:
            prices.append(close)

    return prices


# ==============================================================
# SageMaker Inference (リトライ機能強化)
# ==============================================================

def invoke_sagemaker_with_retry(prices: list, prediction_length: int = 12) -> dict:
    """
    SageMaker Serverless Endpoint を呼び出し（ThrottlingException対応リトライ付き）
    
    ThrottlingExceptionに対して指数バックオフでリトライを実行
    ValidationException/ValidationErrorの場合は即座に例外を再発生（エンドポイント不存在）
    SageMaker Serverlessは同時実行制限があるため、Throttlingは想定内の動作
    Chronos-2 はサンプリング不要のため num_samples パラメータは廃止
    """
    for attempt in range(MAX_RETRIES):
        try:
            return invoke_sagemaker(prices, prediction_length)
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            error_message = str(e)
            
            if error_code == 'ThrottlingException':
                if attempt < MAX_RETRIES - 1:
                    # 指数バックオフ + jitter (SageMaker Serverlessの冷起動を考慮)
                    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                    jitter = random.uniform(0.1, 0.5) * delay
                    total_delay = delay + jitter
                    
                    # INFO: 想定内のThrottlingなのでエラーではなく情報ログとして出力
                    print(f"[INFO] SageMaker throttled (attempt {attempt + 1}/{MAX_RETRIES}), "
                          f"retrying in {total_delay:.1f}s - this is expected behavior")
                    time.sleep(total_delay)
                    continue
                else:
                    print(f"[WARN] SageMaker throttling persisted after {MAX_RETRIES} retries, using fallback")
                    raise
            elif error_code in ['ValidationException', 'ValidationError'] or "not found" in error_message.lower():
                # エンドポイントが存在しない場合は即座に諦める（リトライしない）
                print(f"[ERROR] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' not found: {error_code}")
                raise
            elif error_code == 'AccessDeniedException':
                print(f"[ERROR] Failed to check endpoint '{SAGEMAKER_ENDPOINT}': AccessDeniedException")
                raise
            else:
                # その他のエラーは即座に再発生
                print(f"[ERROR] SageMaker error (non-throttling): {error_code} - {str(e)}")
                raise
                
        except Exception as e:
            # その他の例外（ネットワークエラー等）
            error_message = str(e).lower()
            if "not found" in error_message or "validation" in error_message:
                # エンドポイント関連のエラーは即座に諦める
                print(f"[ERROR] SageMaker endpoint '{SAGEMAKER_ENDPOINT}' not found: {str(e)}")
                raise
            else:
                # その他のエラーは再発生
                print(f"[ERROR] SageMaker unexpected error: {str(e)}")
                raise
    
    # ここには到達しないはずだが、安全のため
    raise Exception("Unexpected error in retry loop")


def invoke_sagemaker(prices: list, prediction_length: int = 12) -> dict:
    """
    SageMaker Serverless Endpoint を呼び出し (Chronos-2)

    Input: {"context": [...], "prediction_length": 12}
    Output: {"median": [...], "mean": [...], "std": [...], "q10": [...], "q90": [...],
             "confidence": 0.7, "model": "chronos-2"}
    """
    payload = {
        "context": prices,
        "prediction_length": prediction_length,
    }

    start_time = time.time()
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=SAGEMAKER_ENDPOINT,
        ContentType="application/json",
        Body=json.dumps(payload),
    )
    elapsed = time.time() - start_time

    raw = json.loads(response["Body"].read().decode("utf-8"))

    # HuggingFace DLC wraps output_fn tuple as [json_string, content_type]
    if isinstance(raw, list) and len(raw) >= 1 and isinstance(raw[0], str):
        result = json.loads(raw[0])
    else:
        result = raw

    print(f"SageMaker invocation: {elapsed:.1f}s, "
          f"confidence={result.get('confidence', 'N/A')}")

    return result


# ==============================================================
# Score Calculation (改善版)
# ==============================================================

def predictions_to_score(result: dict, current_price: float) -> float:
    """
    予測結果をスコアに変換 (-1 to +1)

    改善点 (vs 旧版):
    1. ±3%スケール (旧: ±1%で常に飽和していた)
    2. 外れ値カット (上下10パーセンタイル除去)
    3. 予測の傾き (トレンド加速/減速) も考慮
    """
    median = result.get('median', [])
    if not median or current_price <= 0:
        return 0.0

    n = len(median)

    # 外れ値カット: 予測値が異常に大きい/小さい場合を除去
    valid_predictions = []
    for p in median:
        change_pct = abs(p - current_price) / current_price * 100
        if change_pct < 20:  # ±20%以上の予測は除外
            valid_predictions.append(p)
        else:
            valid_predictions.append(current_price)  # 現在価格で置換

    if not valid_predictions:
        return 0.0

    # 加重平均 (後のステップほど重要)
    n_valid = len(valid_predictions)
    weights = [(i + 1) / n_valid for i in range(n_valid)]
    total_weight = sum(weights)
    weighted_avg = sum(p * w for p, w in zip(valid_predictions, weights)) / total_weight

    # 変化率 → スコア
    change_percent = (weighted_avg - current_price) / current_price * 100

    # ±3%で±1.0にスケール (仮想通貨の1h先では妥当なレンジ)
    score = change_percent / SCORE_SCALE_PERCENT

    # トレンド加速ボーナス: 後半の予測が前半より強い方向に動いている場合
    if n_valid >= 6:
        first_half_avg = sum(valid_predictions[:n_valid // 2]) / (n_valid // 2)
        second_half_avg = sum(valid_predictions[n_valid // 2:]) / (n_valid - n_valid // 2)
        trend_acceleration = (second_half_avg - first_half_avg) / current_price * 100
        # 加速分をスコアに微加算 (最大±0.15)
        accel_bonus = max(-0.15, min(0.15, trend_acceleration / SCORE_SCALE_PERCENT * 0.3))
        score += accel_bonus

    # 確信度による減衰: std が大きい場合はスコアを縮小
    std_values = result.get('std', [])
    if std_values:
        avg_std = sum(std_values) / len(std_values)
        cv = avg_std / current_price  # coefficient of variation
        # CV > 5% → スコアを50%に減衰、CV < 1% → 100%維持
        damping = max(0.5, min(1.0, 1.0 - (cv - 0.01) / 0.04 * 0.5))
        score *= damping

    return max(-1.0, min(1.0, score))


def calculate_momentum_score(prices: list) -> float:
    """モメンタムベースの代替スコア（フォールバック）"""
    if len(prices) < 10:
        return 0.0

    short_momentum = (prices[-1] - prices[-6]) / prices[-6] * 100 if len(prices) >= 6 else 0
    long_momentum = (prices[-1] - prices[-11]) / prices[-11] * 100 if len(prices) >= 11 else 0

    momentum = short_momentum * 0.6 + long_momentum * 0.4

    score = momentum / 2
    return max(-1.0, min(1.0, score))
