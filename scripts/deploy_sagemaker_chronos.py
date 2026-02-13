"""
SageMaker Serverless Endpoint デプロイスクリプト
Chronos-2 (120M) を HuggingFace DLC でデプロイ

boto3のみ使用（sagemaker SDK 不要）

使い方:
    python scripts/deploy_sagemaker_chronos.py

コスト見積もり:
    - Serverless: 使った分だけ課金
    - メモリ: 6144MB (推論時のみ確保)
    - コールドスタート: 30-60秒 (Chronos-2はBoltベースで高速)

変更履歴:
    - v1: Chronos-T5-Base (200M) — サンプリングベース推論
    - v2: Chronos-2 (120M) — 分位数直接出力、250倍高速、5%高精度
"""
import boto3
import json
import os
import sys
import tarfile
import tempfile
import time

# ==============================================================
# 設定
# ==============================================================
REGION = "ap-northeast-1"
ACCOUNT_ID = "652679684315"
ENDPOINT_NAME = "eth-trading-chronos-base"
MODEL_NAME = "eth-trading-chronos-2"
ENDPOINT_CONFIG_NAME = "eth-trading-chronos-2-config"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/eth-trading-sagemaker-execution-role"

# HuggingFace DLC イメージ (PyTorch 2.6.0, Python 3.12, HuggingFace Inference)
# Chronos-2 は torch>=2.2, transformers>=4.41 を要求するため 2.6.0 に更新
HF_IMAGE_URI = f"763104351884.dkr.ecr.{REGION}.amazonaws.com/huggingface-pytorch-inference:2.6.0-transformers4.49.0-cpu-py312-ubuntu22.04"

# Serverless設定
MEMORY_SIZE_MB = 6144  # 6GB (Chronos-2 + PyTorch CPU)
MAX_CONCURRENCY = 8    # 最大同時実行数 (クォータ10以内)

MODEL_BUCKET = f"eth-trading-sagemaker-models-{ACCOUNT_ID}"
MODEL_S3_KEY = "chronos-2/model.tar.gz"


def create_inference_code():
    """SageMaker推論コード (inference.py) を生成 — Chronos-2版"""
    return '''
import json
import torch
import numpy as np

# グローバルにモデルをキャッシュ
_pipeline = None

def model_fn(model_dir):
    """モデルロード — Chronos-2"""
    global _pipeline
    if _pipeline is None:
        from chronos import BaseChronosPipeline
        _pipeline = BaseChronosPipeline.from_pretrained(
            "amazon/chronos-2",
            device_map="cpu",
            torch_dtype=torch.float32,
        )
        print(f"[INFO] Model loaded: {type(_pipeline).__name__}", flush=True)
    return _pipeline

def input_fn(request_body, request_content_type):
    """入力パース"""
    if request_content_type == "application/json":
        return json.loads(request_body)
    raise ValueError(f"Unsupported content type: {request_content_type}")

def predict_fn(data, model):
    """
    推論実行 — Chronos-2 (分位数直接出力)
    出力フォーマットは旧版と互換性を維持: median, mean, std, confidence
    """
    # Chronos-2 は 3D: (batch, variates, time)
    context = torch.tensor([[data["context"]]], dtype=torch.float32)  # (1, 1, T)
    prediction_length = data.get("prediction_length", 12)

    # predict_quantiles → tuple(list[Tensor], list[Tensor])
    #   quantiles_list[i]: (n_variates, prediction_length, n_quantile_levels)
    #   mean_list[i]:      (n_variates, prediction_length)
    quantiles_list, mean_list = model.predict_quantiles(
        context,
        prediction_length=prediction_length,
        quantile_levels=[0.1, 0.5, 0.9],
        limit_prediction_length=False,
    )

    # 最初のシリーズ(唯一)、最初のvariate(univariate)
    q = quantiles_list[0][0]  # (prediction_length, 3)
    m = mean_list[0][0]       # (prediction_length,)

    q10 = q[:, 0].numpy().tolist()     # 10th percentile
    median = q[:, 1].numpy().tolist()  # 50th percentile
    q90 = q[:, 2].numpy().tolist()     # 90th percentile
    mean = m.numpy().tolist()

    # std: 分位数の広がりから推定 (正規分布近似: q90-q10 = 2.56 sigma)
    std = [abs(q90[i] - q10[i]) / 2.56 for i in range(len(q10))]

    # 確信度: 予測区間の狭さベース
    confidence_per_step = []
    for i in range(len(median)):
        if abs(median[i]) > 1e-8:
            cv = std[i] / abs(median[i])
            confidence_per_step.append(round(1.0 / (1.0 + cv * 10), 3))
        else:
            confidence_per_step.append(0.5)

    return {
        "median": median,
        "mean": mean,
        "std": std,
        "q10": q10,
        "q90": q90,
        "confidence_per_step": confidence_per_step,
        "confidence": round(float(np.mean(confidence_per_step)), 3),
        "model": "chronos-2",
    }

def output_fn(prediction, accept):
    """出力フォーマット"""
    return json.dumps(prediction), "application/json"
'''


def package_model_artifacts():
    """model.tar.gz を作成 (code/ ディレクトリのみ)"""
    tmpdir = tempfile.mkdtemp()
    model_tar_path = os.path.join(tmpdir, "model.tar.gz")

    code_dir = os.path.join(tmpdir, "code")
    os.makedirs(code_dir, exist_ok=True)

    # inference.py
    with open(os.path.join(code_dir, "inference.py"), "w", encoding="utf-8") as f:
        f.write(create_inference_code())

    # requirements.txt (Chronos-2 は cronos-forecasting 2.x 以降が必要)
    # torch は DLC (2.6.0) にプリインストール済みなので不要
    with open(os.path.join(code_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        f.write("chronos-forecasting>=2.2.0\nnumpy\n")

    # tar.gz 作成
    with tarfile.open(model_tar_path, "w:gz") as tar:
        tar.add(code_dir, arcname="code")

    print(f"  model.tar.gz created: {model_tar_path}")
    return model_tar_path


def create_sagemaker_role():
    """SageMaker実行ロールを作成 (存在しない場合)"""
    iam = boto3.client("iam", region_name=REGION)
    role_name = "eth-trading-sagemaker-execution-role"

    try:
        resp = iam.get_role(RoleName=role_name)
        print(f"  IAM Role '{role_name}' already exists.")
        return resp["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        pass

    print(f"  Creating IAM Role '{role_name}'...")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "sagemaker.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description="SageMaker execution role for Chronos inference",
    )

    for policy_arn in [
        "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
        "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    ]:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

    print(f"  Role created. Waiting 10s for propagation...")
    time.sleep(10)
    return ROLE_ARN


def cleanup_existing(sm_client):
    """既存リソースの削除 (旧T5モデルも含む)"""
    # Endpoint
    try:
        sm_client.describe_endpoint(EndpointName=ENDPOINT_NAME)
        print(f"  Deleting existing endpoint '{ENDPOINT_NAME}'...")
        sm_client.delete_endpoint(EndpointName=ENDPOINT_NAME)
        waiter = sm_client.get_waiter("endpoint_deleted")
        waiter.wait(EndpointName=ENDPOINT_NAME, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        print("  Endpoint deleted.")
    except sm_client.exceptions.ClientError:
        print("  No existing endpoint.")

    # Endpoint Config — 新旧両方削除
    for config_name in [ENDPOINT_CONFIG_NAME, "eth-trading-chronos-base-config-v2", "eth-trading-chronos-base-config"]:
        try:
            sm_client.delete_endpoint_config(EndpointConfigName=config_name)
            print(f"  Deleted endpoint config: {config_name}")
        except sm_client.exceptions.ClientError:
            pass

    # Model — 新旧両方削除
    for model_name in [MODEL_NAME, "eth-trading-chronos-base"]:
        try:
            sm_client.delete_model(ModelName=model_name)
            print(f"  Deleted model: {model_name}")
        except sm_client.exceptions.ClientError:
            pass


def deploy():
    """SageMaker Serverless エンドポイントをデプロイ (boto3のみ)"""
    print("=" * 60)
    print("SageMaker Chronos-2 Serverless Endpoint Deploy")
    print("=" * 60)

    sm_client = boto3.client("sagemaker", region_name=REGION)
    s3_client = boto3.client("s3", region_name=REGION)

    # 1. IAM Role
    print("\n[1/5] Checking SageMaker execution role...")
    create_sagemaker_role()

    # 2. 推論コードをパッケージング & S3アップロード
    print("\n[2/5] Packaging and uploading inference code...")
    model_tar_path = package_model_artifacts()
    s3_client.upload_file(model_tar_path, MODEL_BUCKET, MODEL_S3_KEY)
    model_data_url = f"s3://{MODEL_BUCKET}/{MODEL_S3_KEY}"
    print(f"  Uploaded: {model_data_url}")

    # 3. 既存リソース削除
    print("\n[3/5] Cleaning up existing resources...")
    cleanup_existing(sm_client)

    # 4. SageMaker Model 作成
    print("\n[4/5] Creating SageMaker model...")
    sm_client.create_model(
        ModelName=MODEL_NAME,
        PrimaryContainer={
            "Image": HF_IMAGE_URI,
            "ModelDataUrl": model_data_url,
            "Environment": {
                "HF_MODEL_ID": "amazon/chronos-2",
                "HF_TASK": "time-series-forecasting",
                "SAGEMAKER_MODEL_SERVER_TIMEOUT": "300",
            },
        },
        ExecutionRoleArn=ROLE_ARN,
    )
    print(f"  Model created: {MODEL_NAME}")

    # 5. Endpoint Config (Serverless) + Endpoint 作成
    print("\n[5/5] Creating Serverless endpoint...")
    sm_client.create_endpoint_config(
        EndpointConfigName=ENDPOINT_CONFIG_NAME,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": MODEL_NAME,
                "ServerlessConfig": {
                    "MemorySizeInMB": MEMORY_SIZE_MB,
                    "MaxConcurrency": MAX_CONCURRENCY,
                },
            }
        ],
    )
    print(f"  Endpoint config created: {ENDPOINT_CONFIG_NAME}")

    sm_client.create_endpoint(
        EndpointName=ENDPOINT_NAME,
        EndpointConfigName=ENDPOINT_CONFIG_NAME,
    )
    print(f"  Endpoint creation started: {ENDPOINT_NAME}")
    print("  Waiting for endpoint to be InService (this may take 5-10 minutes)...")

    # 完了待ち
    waiter = sm_client.get_waiter("endpoint_in_service")
    try:
        waiter.wait(
            EndpointName=ENDPOINT_NAME,
            WaiterConfig={"Delay": 30, "MaxAttempts": 40}  # 最大20分
        )
        print(f"\n{'=' * 60}")
        print(f"Endpoint deployed: {ENDPOINT_NAME}")
        print(f"Memory: {MEMORY_SIZE_MB}MB, Max Concurrency: {MAX_CONCURRENCY}")
        print(f"{'=' * 60}")
    except Exception as e:
        # ステータス確認
        resp = sm_client.describe_endpoint(EndpointName=ENDPOINT_NAME)
        status = resp["EndpointStatus"]
        print(f"\n  Endpoint status: {status}")
        if status == "Failed":
            print(f"  Failure reason: {resp.get('FailureReason', 'unknown')}")
            return None
        elif status == "Creating":
            print("  Still creating. Check AWS Console for progress.")
        raise

    # テスト呼び出し
    print("\n[Test] Invoking endpoint (first call = cold start, may take 60-120s)...")
    runtime = boto3.client("sagemaker-runtime", region_name=REGION)
    test_data = {
        "context": [100.0 + i * 0.5 for i in range(60)],
        "prediction_length": 12,
    }
    try:
        response = runtime.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            ContentType="application/json",
            Body=json.dumps(test_data),
        )
        raw = json.loads(response["Body"].read().decode("utf-8"))
        # HuggingFace DLC wraps output_fn tuple as [json_string, content_type]
        if isinstance(raw, list) and len(raw) >= 1 and isinstance(raw[0], str):
            result = json.loads(raw[0])
        else:
            result = raw
        print(f"  Median (first 3): {result['median'][:3]}")
        print(f"  Confidence: {result['confidence']}")
        print(f"  Model: {result.get('model', 'unknown')}")
        print("  Test PASSED")
    except Exception as e:
        print(f"  Test failed (cold start may need more time): {e}")
        print("  Endpoint is deployed. Retry in 1-2 minutes.")

    return ENDPOINT_NAME


if __name__ == "__main__":
    deploy()
