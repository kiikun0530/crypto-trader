"""
SageMaker Serverless Endpoint デプロイスクリプト
Chronos-T5-Base (200M) を HuggingFace DLC でデプロイ

boto3のみ使用（sagemaker SDK 不要）

使い方:
    python scripts/deploy_sagemaker_chronos.py

コスト見積もり:
    - Serverless: 使った分だけ課金 (~$5-15/月)
    - メモリ: 6144MB (推論時のみ確保)
    - コールドスタート: 60-120秒 (Step Functions タイムアウト 300秒以内)
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
MODEL_NAME = "eth-trading-chronos-base"
ENDPOINT_CONFIG_NAME = "eth-trading-chronos-base-config"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/eth-trading-sagemaker-execution-role"

# HuggingFace DLC イメージ (PyTorch 2.1.0, Python 3.10, HuggingFace Inference)
# ap-northeast-1 の HuggingFace Inference CPU イメージ
HF_IMAGE_URI = f"763104351884.dkr.ecr.{REGION}.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-cpu-py310-ubuntu22.04"

# Serverless設定
MEMORY_SIZE_MB = 6144  # 6GB (Chronos-Base + PyTorch CPU)
MAX_CONCURRENCY = 2    # 最大同時実行数

MODEL_BUCKET = f"eth-trading-sagemaker-models-{ACCOUNT_ID}"
MODEL_S3_KEY = "chronos-base/model.tar.gz"


def create_inference_code():
    """SageMaker推論コード (inference.py) を生成"""
    return '''
import json
import torch
import numpy as np
from chronos import ChronosPipeline

# グローバルにモデルをキャッシュ
_pipeline = None

def model_fn(model_dir):
    """モデルロード"""
    global _pipeline
    if _pipeline is None:
        _pipeline = ChronosPipeline.from_pretrained(
            "amazon/chronos-t5-base",
            device_map="cpu",
            torch_dtype=torch.float32,
        )
    return _pipeline

def input_fn(request_body, request_content_type):
    """入力パース"""
    if request_content_type == "application/json":
        return json.loads(request_body)
    raise ValueError(f"Unsupported content type: {request_content_type}")

def predict_fn(data, model):
    """推論実行"""
    context = torch.tensor([data["context"]], dtype=torch.float32)
    prediction_length = data.get("prediction_length", 12)
    num_samples = data.get("num_samples", 50)

    # Chronos推論 (確率的サンプリング)
    forecast = model.predict(
        context,
        prediction_length,
        num_samples=num_samples,
        limit_prediction_length=False,
    )
    # forecast shape: (1, num_samples, prediction_length)
    samples = forecast[0].numpy()  # (num_samples, prediction_length)

    # 統計計算
    median = np.median(samples, axis=0).tolist()
    mean = np.mean(samples, axis=0).tolist()
    std = np.std(samples, axis=0).tolist()

    # 各ステップの確信度 (coefficient of variation ベース)
    confidence_per_step = []
    for i in range(prediction_length):
        if abs(median[i]) > 1e-8:
            cv = std[i] / abs(median[i])
            confidence_per_step.append(round(1.0 / (1.0 + cv * 10), 3))
        else:
            confidence_per_step.append(0.5)

    return {
        "median": median,
        "mean": mean,
        "std": std,
        "confidence_per_step": confidence_per_step,
        "confidence": round(float(np.mean(confidence_per_step)), 3),
        "num_samples": num_samples,
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
    with open(os.path.join(code_dir, "inference.py"), "w") as f:
        f.write(create_inference_code())

    # requirements.txt
    with open(os.path.join(code_dir, "requirements.txt"), "w") as f:
        f.write("chronos-forecasting[inference]>=1.3.0\ntorch>=2.0.0\nnumpy\n")

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
    """既存リソースの削除"""
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

    # Endpoint Config
    try:
        sm_client.delete_endpoint_config(EndpointConfigName=ENDPOINT_CONFIG_NAME)
        print("  Deleted existing endpoint config.")
    except sm_client.exceptions.ClientError:
        pass

    # Model
    try:
        sm_client.delete_model(ModelName=MODEL_NAME)
        print("  Deleted existing model.")
    except sm_client.exceptions.ClientError:
        pass


def deploy():
    """SageMaker Serverless エンドポイントをデプロイ (boto3のみ)"""
    print("=" * 60)
    print("SageMaker Chronos-Base Serverless Endpoint Deploy")
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
                "HF_MODEL_ID": "amazon/chronos-t5-base",
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
        "num_samples": 5,
    }
    try:
        response = runtime.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            ContentType="application/json",
            Body=json.dumps(test_data),
        )
        result = json.loads(response["Body"].read().decode("utf-8"))
        print(f"  Median (first 3): {result['median'][:3]}")
        print(f"  Confidence: {result['confidence']}")
        print("  Test PASSED")
    except Exception as e:
        print(f"  Test failed (cold start may need more time): {e}")
        print("  Endpoint is deployed. Retry in 1-2 minutes.")

    return ENDPOINT_NAME


if __name__ == "__main__":
    deploy()
