"""
SageMaker Serverless Endpoint デプロイスクリプト
Chronos-T5-Base (200M) を HuggingFace DLC でデプロイ

使い方:
    pip install sagemaker boto3
    python scripts/deploy_sagemaker_chronos.py

コスト見積もり:
    - Serverless: 使った分だけ課金 (~$5-15/月)
    - メモリ: 6144MB (推論時のみ確保)
    - コールドスタート: 60-120秒 (Step Functions タイムアウト 300秒以内)
"""
import boto3
import sagemaker
from sagemaker.huggingface import HuggingFaceModel
from sagemaker.serverless import ServerlessInferenceConfig
import json
import os
import sys
import tarfile
import tempfile

# ==============================================================
# 設定
# ==============================================================
REGION = "ap-northeast-1"
ACCOUNT_ID = "652679684315"
ENDPOINT_NAME = "eth-trading-chronos-base"
MODEL_NAME = "eth-trading-chronos-base"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/eth-trading-sagemaker-execution-role"

# Serverless設定
MEMORY_SIZE_MB = 6144  # 6GB (Chronos-Base + PyTorch CPU)
MAX_CONCURRENCY = 2    # 最大同時実行数 (6通貨だが直列実行なので2で十分)


def create_inference_code():
    """SageMaker推論コード (inference.py) を生成"""
    inference_code = '''
import json
import torch
import numpy as np
from chronos import ChronosPipeline

def model_fn(model_dir):
    """モデルロード"""
    pipeline = ChronosPipeline.from_pretrained(
        "amazon/chronos-t5-base",
        device_map="cpu",
        torch_dtype=torch.float32,
    )
    return pipeline

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
    # 各ステップの予測分散比率 (確信度指標)
    # std / |median| が小さいほど確信度が高い
    confidence_per_step = []
    for i in range(prediction_length):
        if abs(median[i]) > 1e-8:
            cv = std[i] / abs(median[i])  # coefficient of variation
            confidence_per_step.append(round(1.0 / (1.0 + cv * 10), 3))  # 0~1
        else:
            confidence_per_step.append(0.5)

    return {
        "median": median,
        "mean": mean,
        "std": std,
        "samples": samples.tolist(),
        "confidence_per_step": confidence_per_step,
        "confidence": round(float(np.mean(confidence_per_step)), 3),
        "num_samples": num_samples,
    }

def output_fn(prediction, accept):
    """出力フォーマット"""
    return json.dumps(prediction), "application/json"
'''
    return inference_code


def create_requirements():
    """requirements.txt"""
    return "chronos-forecasting[inference]>=1.3.0\ntorch>=2.0.0\nnumpy\n"


def package_model_artifacts():
    """model.tar.gz を作成"""
    tmpdir = tempfile.mkdtemp()
    model_tar_path = os.path.join(tmpdir, "model.tar.gz")

    code_dir = os.path.join(tmpdir, "code")
    os.makedirs(code_dir, exist_ok=True)

    # inference.py
    with open(os.path.join(code_dir, "inference.py"), "w") as f:
        f.write(create_inference_code())

    # requirements.txt
    with open(os.path.join(code_dir, "requirements.txt"), "w") as f:
        f.write(create_requirements())

    # tar.gz 作成 (code/ ディレクトリを含む)
    with tarfile.open(model_tar_path, "w:gz") as tar:
        tar.add(code_dir, arcname="code")

    return model_tar_path


def deploy():
    """SageMaker Serverless エンドポイントをデプロイ"""
    print("=" * 60)
    print("SageMaker Chronos-Base Serverless Endpoint Deploy")
    print("=" * 60)

    session = boto3.Session(region_name=REGION)
    sm_session = sagemaker.Session(boto_session=session)
    sm_client = session.client("sagemaker")
    s3_client = session.client("s3")

    # 1. 推論コードをパッケージング
    print("\n[1/4] Packaging inference code...")
    model_tar_path = package_model_artifacts()
    print(f"  Created: {model_tar_path}")

    # 2. S3にアップロード
    bucket = f"eth-trading-sagemaker-models-{ACCOUNT_ID}"
    s3_key = "chronos-base/model.tar.gz"
    print(f"\n[2/4] Uploading to s3://{bucket}/{s3_key}...")
    s3_client.upload_file(model_tar_path, bucket, s3_key)
    model_data_url = f"s3://{bucket}/{s3_key}"
    print(f"  Uploaded: {model_data_url}")

    # 3. 既存エンドポイントの削除 (存在する場合)
    print(f"\n[3/4] Checking existing endpoint '{ENDPOINT_NAME}'...")
    try:
        sm_client.describe_endpoint(EndpointName=ENDPOINT_NAME)
        print("  Existing endpoint found, deleting...")
        sm_client.delete_endpoint(EndpointName=ENDPOINT_NAME)
        print("  Waiting for deletion...")
        waiter = sm_client.get_waiter("endpoint_deleted")
        waiter.wait(EndpointName=ENDPOINT_NAME)
        print("  Deleted.")
    except sm_client.exceptions.ClientError:
        print("  No existing endpoint.")

    # 既存モデルの削除
    try:
        sm_client.delete_model(ModelName=MODEL_NAME)
        print("  Deleted existing model.")
    except sm_client.exceptions.ClientError:
        pass

    # 既存エンドポイント設定の削除
    try:
        sm_client.delete_endpoint_config(EndpointConfigName=ENDPOINT_NAME)
        print("  Deleted existing endpoint config.")
    except sm_client.exceptions.ClientError:
        pass

    # 4. HuggingFace Model + Serverless Deploy
    print(f"\n[4/4] Deploying Chronos-Base to Serverless Endpoint...")

    hub_env = {
        "HF_MODEL_ID": "amazon/chronos-t5-base",
        "HF_TASK": "time-series-forecasting",
        "SAGEMAKER_MODEL_SERVER_TIMEOUT": "300",
    }

    huggingface_model = HuggingFaceModel(
        model_data=model_data_url,
        role=ROLE_ARN,
        transformers_version="4.37.0",
        pytorch_version="2.1.0",
        py_version="py310",
        env=hub_env,
        name=MODEL_NAME,
        sagemaker_session=sm_session,
    )

    serverless_config = ServerlessInferenceConfig(
        memory_size_in_mb=MEMORY_SIZE_MB,
        max_concurrency=MAX_CONCURRENCY,
    )

    predictor = huggingface_model.deploy(
        serverless_inference_config=serverless_config,
        endpoint_name=ENDPOINT_NAME,
    )

    print(f"\n{'=' * 60}")
    print(f"Endpoint deployed: {ENDPOINT_NAME}")
    print(f"Memory: {MEMORY_SIZE_MB}MB, Max Concurrency: {MAX_CONCURRENCY}")
    print(f"{'=' * 60}")

    # テスト呼び出し
    print("\n[Test] Invoking endpoint...")
    test_data = {
        "context": [100.0 + i * 0.5 for i in range(60)],
        "prediction_length": 12,
        "num_samples": 5,
    }
    try:
        result = predictor.predict(test_data)
        print(f"  Median predictions: {result['median'][:3]}...")
        print(f"  Confidence: {result['confidence']}")
        print("  Test PASSED ✓")
    except Exception as e:
        print(f"  Test failed (may need cold start wait): {e}")
        print("  Endpoint is deployed. Retry test after 1-2 minutes.")

    return ENDPOINT_NAME


def create_sagemaker_role():
    """SageMaker実行ロールを作成 (存在しない場合)"""
    iam = boto3.client("iam", region_name=REGION)
    role_name = "eth-trading-sagemaker-execution-role"

    try:
        iam.get_role(RoleName=role_name)
        print(f"IAM Role '{role_name}' already exists.")
        return
    except iam.exceptions.NoSuchEntityException:
        pass

    print(f"Creating IAM Role '{role_name}'...")

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

    # S3アクセス (モデル読み込み)
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
    )

    # CloudWatch Logs
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
    )

    # ECR (コンテナイメージ取得)
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    )

    print(f"  Role created: {ROLE_ARN}")


if __name__ == "__main__":
    print("Pre-flight: Checking SageMaker execution role...")
    create_sagemaker_role()
    print()
    deploy()
