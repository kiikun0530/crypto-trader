"""
Amazon Chronos-T5-Tiny 推論ハンドラー
SageMaker Serverless Inference 用

入力:  {"prices": [100.0, 101.5, ...], "prediction_length": 12}
出力:  {"predictions": [102.3, ...], "current_price": 101.5}
"""
import json
import os
import torch
import numpy as np


def model_fn(model_dir):
    """モデルロード（HuggingFace Hub からダウンロード）"""
    from chronos import ChronosPipeline

    pipeline = ChronosPipeline.from_pretrained(
        "amazon/chronos-t5-tiny",
        device_map="cpu",
        torch_dtype=torch.float32,
    )
    return pipeline


def input_fn(request_body, request_content_type):
    """リクエスト解析"""
    if request_content_type == "application/json":
        return json.loads(request_body)
    raise ValueError(f"Unsupported content type: {request_content_type}")


def predict_fn(data, model):
    """予測実行"""
    prices = data.get("prices", [])
    prediction_length = data.get("prediction_length", 12)

    if not prices or len(prices) < 10:
        return {
            "predictions": [],
            "current_price": prices[-1] if prices else 0,
            "error": "insufficient_data",
        }

    context = torch.tensor([prices], dtype=torch.float32)

    # 予測（確率的予測 → 中央値を採用）
    forecast = model.predict(context, prediction_length)
    # forecast shape: (1, num_samples, prediction_length)
    median = np.median(forecast[0].numpy(), axis=0).tolist()

    return {
        "predictions": median,
        "current_price": prices[-1],
    }


def output_fn(prediction, response_content_type):
    """レスポンス生成"""
    return json.dumps(prediction)
