"""
Chronos呼び出し Lambda (ONNX Runtime版)
Lambda Layer上の ONNX Runtime で Chronos-T5-Tiny を直接推論
S3からモデルをコールドスタート時にダウンロード、/tmp にキャッシュ

トークナイゼーション:
- MeanScaleUniformBins 方式を再実装
- 入力: 連続値 → scale正規化 → bucketize → token_ids
- 出力: token_ids → centers lookup → scale復元 → 連続値
"""
import json
import os
import time
import boto3
import numpy as np

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
MODEL_BUCKET = os.environ.get('MODEL_BUCKET', 'eth-trading-sagemaker-models-652679684315')
MODEL_PREFIX = os.environ.get('MODEL_PREFIX', 'chronos-onnx')
PREDICTION_LENGTH = int(os.environ.get('PREDICTION_LENGTH', '12'))
NUM_SAMPLES = int(os.environ.get('NUM_SAMPLES', '20'))

# Chronos-T5-Tiny tokenizer設定
N_SPECIAL_TOKENS = 2
PAD_TOKEN_ID = 0
EOS_TOKEN_ID = 1
N_TOKENS = 4096
CONTEXT_LENGTH = 512

# グローバルキャッシュ（Lambda warm start時に再利用）
_model_cache = {}


def handler(event, context):
    """Chronos ONNX予測取得"""
    pair = event.get('pair', 'eth_usdt')

    try:
        prices = get_price_history(pair, limit=60)

        if not prices:
            return {
                'pair': pair,
                'chronos_score': 0.0,
                'prediction': None,
                'reason': 'no_data',
                'current_price': 0
            }

        # ONNX モデルで推論
        try:
            predictions = run_onnx_inference(prices, PREDICTION_LENGTH, NUM_SAMPLES)
            if predictions is not None and len(predictions) > 0:
                score = predictions_to_score(predictions, prices[-1])
                print(f"ONNX inference OK: {pair}, score={score:.3f}, predictions={len(predictions)}")
            else:
                print(f"ONNX inference returned empty, fallback to momentum: {pair}")
                score = calculate_momentum_score(prices)
                predictions = None
        except Exception as e:
            print(f"ONNX inference failed: {e}, fallback to momentum: {pair}")
            score = calculate_momentum_score(prices)
            predictions = None

        return {
            'pair': pair,
            'chronos_score': round(score, 3),
            'prediction': predictions,
            'current_price': prices[-1] if prices else 0
        }

    except Exception as e:
        print(f"Error in chronos-caller: {str(e)}")
        import traceback
        traceback.print_exc()

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


# ==============================================================
# ONNX Model Loading & Inference
# ==============================================================

def load_model():
    """S3からONNXモデル + tokenizer dataをダウンロードしてキャッシュ"""
    if 'encoder' in _model_cache:
        return _model_cache

    import onnxruntime as ort

    model_dir = '/tmp/chronos-onnx'
    os.makedirs(model_dir, exist_ok=True)

    # S3からファイルダウンロード
    files = [
        'encoder_model.onnx',
        'decoder_model.onnx',
        'decoder_with_past_model.onnx',
        'centers.json',
        'boundaries.json',
        'config.json',
    ]

    start = time.time()
    for f in files:
        local_path = os.path.join(model_dir, f)
        if not os.path.exists(local_path):
            s3.download_file(MODEL_BUCKET, f'{MODEL_PREFIX}/{f}', local_path)
    download_time = time.time() - start

    # ONNX Sessions 作成
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_options.inter_op_num_threads = 1
    sess_options.intra_op_num_threads = 1

    _model_cache['encoder'] = ort.InferenceSession(
        os.path.join(model_dir, 'encoder_model.onnx'),
        sess_options,
        providers=['CPUExecutionProvider']
    )
    _model_cache['decoder'] = ort.InferenceSession(
        os.path.join(model_dir, 'decoder_model.onnx'),
        sess_options,
        providers=['CPUExecutionProvider']
    )

    # decoder_with_past は KV-Cache付き高速デコード用
    decoder_past_path = os.path.join(model_dir, 'decoder_with_past_model.onnx')
    if os.path.exists(decoder_past_path):
        _model_cache['decoder_with_past'] = ort.InferenceSession(
            decoder_past_path,
            sess_options,
            providers=['CPUExecutionProvider']
        )

    # Tokenizer data
    with open(os.path.join(model_dir, 'centers.json')) as f:
        _model_cache['centers'] = np.array(json.load(f), dtype=np.float32)

    with open(os.path.join(model_dir, 'boundaries.json')) as f:
        _model_cache['boundaries'] = np.array(json.load(f), dtype=np.float32)

    print(f"ONNX model loaded in {download_time:.1f}s (download) + {time.time()-start-download_time:.1f}s (init)")
    return _model_cache


def tokenize_input(prices: list, model_data: dict) -> tuple:
    """
    Chronos MeanScaleUniformBins tokenization (再実装)

    1. Mean absolute scaling
    2. Bucketize with uniform bins
    3. Add special tokens offset
    """
    context = np.array(prices, dtype=np.float32)

    # Truncate to context_length
    if len(context) > CONTEXT_LENGTH:
        context = context[-CONTEXT_LENGTH:]

    # Attention mask (NaN → False)
    attention_mask = ~np.isnan(context)

    # Mean absolute scale
    abs_values = np.abs(context) * attention_mask
    scale = np.sum(abs_values) / np.sum(attention_mask)
    if scale <= 0:
        scale = 1.0

    # Scale context
    scaled_context = context / scale

    # Bucketize
    boundaries = model_data['boundaries']
    token_ids = np.searchsorted(boundaries, scaled_context, side='right').astype(np.int64)
    token_ids = token_ids + N_SPECIAL_TOKENS
    token_ids = np.clip(token_ids, 0, N_TOKENS - 1)

    # NaN → pad
    token_ids[~attention_mask] = PAD_TOKEN_ID

    # Append EOS token (for seq2seq)
    token_ids = np.append(token_ids, EOS_TOKEN_ID)
    attention_mask = np.append(attention_mask, True)

    # Add batch dimension
    token_ids = token_ids.reshape(1, -1)
    attention_mask = attention_mask.reshape(1, -1).astype(np.int64)

    return token_ids, attention_mask, float(scale)


def detokenize_output(token_ids: np.ndarray, scale: float, model_data: dict) -> np.ndarray:
    """
    Chronos output_transform (再実装)

    token_ids → centers lookup → scale復元
    """
    centers = model_data['centers']
    indices = np.clip(token_ids - N_SPECIAL_TOKENS - 1, 0, len(centers) - 1).astype(np.int64)
    return centers[indices] * scale


def run_onnx_inference(prices: list, prediction_length: int = 12, num_samples: int = 20) -> list:
    """
    ONNX Runtime で Chronos-T5-Tiny 推論

    Top-k sampling で確率的予測を生成し、中央値を返す
    """
    model_data = load_model()
    encoder_session = model_data['encoder']
    decoder_session = model_data['decoder']

    # Tokenize
    token_ids, attention_mask, scale = tokenize_input(prices, model_data)

    # Encoder
    encoder_outputs = encoder_session.run(
        None,
        {
            'input_ids': token_ids,
            'attention_mask': attention_mask,
        }
    )
    encoder_hidden = encoder_outputs[0]  # (1, seq_len, hidden_dim)

    # Decoder: generate predictions autoregressively
    all_samples = []

    # Get decoder input names
    decoder_input_names = [inp.name for inp in decoder_session.get_inputs()]

    for sample_idx in range(num_samples):
        generated_tokens = []
        decoder_input = np.array([[PAD_TOKEN_ID]], dtype=np.int64)

        for step in range(prediction_length):
            # Build decoder inputs
            decoder_feeds = {}
            for name in decoder_input_names:
                if name == 'input_ids':
                    decoder_feeds['input_ids'] = token_ids
                elif name == 'decoder_input_ids':
                    decoder_feeds['decoder_input_ids'] = decoder_input
                elif name == 'attention_mask':
                    decoder_feeds['attention_mask'] = attention_mask
                elif name == 'encoder_hidden_states':
                    decoder_feeds['encoder_hidden_states'] = encoder_hidden
                elif 'encoder_attention_mask' in name:
                    decoder_feeds[name] = attention_mask

            decoder_outputs = decoder_session.run(None, decoder_feeds)
            logits = decoder_outputs[0]  # (1, seq_len, vocab_size)

            # Get logits for last position
            next_logits = logits[0, -1, :]  # (vocab_size,)

            # Top-k sampling with temperature
            temperature = 1.0
            top_k = 50
            next_logits = next_logits / temperature

            # Top-k filtering
            top_k_indices = np.argsort(next_logits)[-top_k:]
            top_k_logits = next_logits[top_k_indices]

            # Softmax
            exp_logits = np.exp(top_k_logits - np.max(top_k_logits))
            probs = exp_logits / np.sum(exp_logits)

            # Sample
            chosen_idx = np.random.choice(len(probs), p=probs)
            next_token = top_k_indices[chosen_idx]

            generated_tokens.append(int(next_token))
            decoder_input = np.array([generated_tokens], dtype=np.int64)

        all_samples.append(generated_tokens)

    # Detokenize: token_ids → prices
    all_samples_np = np.array(all_samples)  # (num_samples, prediction_length)
    all_predictions = detokenize_output(all_samples_np, scale, model_data)

    # Median across samples
    median_predictions = np.median(all_predictions, axis=0).tolist()

    return median_predictions


# ==============================================================
# Score Calculation
# ==============================================================

def predictions_to_score(predictions: list, current_price: float) -> float:
    """
    予測価格列をスコアに変換 (-1 to +1)

    ロジック:
    - 予測系列の加重平均を計算（直近予測を軽く、遠い予測を重く）
    - 現在価格との変化率を計算
    - ±5% の変動で ±1.0 にスケール（暗号通貨の3時間予測窓に適切）
    """
    if not predictions or current_price <= 0:
        return 0.0

    n = len(predictions)
    weights = [(i + 1) / n for i in range(n)]
    total_weight = sum(weights)

    weighted_avg = sum(p * w for p, w in zip(predictions, weights)) / total_weight

    change_percent = (weighted_avg - current_price) / current_price * 100

    # ±5%の変動で±1.0にスケール（±3%では暗号通貨のボラティリティに対してクリップが頻発するため拡張）
    score = change_percent / 5.0
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
