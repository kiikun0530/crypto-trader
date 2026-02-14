# ETH Trading Bot - Terraform Infrastructure

このディレクトリには、ETH Trading Botの全AWSインフラをコード化したTerraform設定が含まれています。

## アーキテクチャ概要

```
EventBridge (スケジュール)
    ↓
Lambda (9関数)  ← VPC外で実行（コスト最適化）
    ↓
Step Functions (分析ワークフロー)
    ↓
DynamoDB (6テーブル) / S3 / SQS
    ↓
SNS → Slack通知
```

## コスト設計

### 現在の構成（Lambda VPC外実行）

| リソース | 月額コスト |
|---------|-----------|
| Lambda (9関数) | ~$1-2 |
| DynamoDB (オンデマンド) | ~$0-5 |
| S3 (アーカイブ) | ~$0.1 |
| Step Functions | ~$0.5 |
| EventBridge | ~$0 |
| SNS/SQS | ~$0 |
| **合計** | **~$2-8/月** |

### コスト最適化の経緯 (2026-02-07)

元々LambdaはVPC内で実行していましたが、以下のコストが発生：

| リソース | 月額コスト |
|---------|-----------|
| NAT Gateway | ~$45 |
| Elastic IP | ~$3.6 |
| **VPC関連合計** | **~$48.6/月** |

**判断**: LambdaはDynamoDB/S3/SNS等のAWSマネージドサービスにのみアクセスするため、
VPC内にある必要がない。IAMロールでアクセス制御されているため、セキュリティ上も問題なし。

**結果**: 月額コストを約95%削減

## 使用方法

### 初回デプロイ

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

### 環境変数

`terraform.tfvars` に以下を設定：

```hcl
aws_region        = "ap-northeast-1"
environment       = "production"
slack_webhook_url = "https://hooks.slack.com/services/xxx/xxx/xxx"
```

## VPC移行ガイド

### Lambda VPC内 → VPC外への移行

LambdaをVPC内からVPC外に移行する際、AWS Lambda VPC ENI (Hyperplane ENI) の
削除に最大20分かかります。この間、Security Groupは削除できません。

#### 方法1: time_sleep/null_resource を使用（推奨）

```hcl
# terraform.tfvars に追加
enable_eni_cleanup = true
```

その後、通常通り `terraform apply` を実行。ENI削除を自動的に待機します。

#### 方法2: 段階的にapply

```bash
# Step 1: Lambda関数のVPC設定を更新
terraform apply -target=aws_lambda_function.functions

# Step 2: ENI削除を待機（5-20分）
# AWSコンソールまたはCLIでENIの状態を監視

# Step 3: 残りのリソース（SG削除など）を適用
terraform apply
```

#### 方法3: 手動ENI削除

```powershell
# ENI確認
aws ec2 describe-network-interfaces --filters "Name=description,Values=AWS Lambda VPC ENI*" --query "NetworkInterfaces[*].{ID:NetworkInterfaceId,Status:Status}" --output table

# ENI削除（StatusがavailableのENIのみ削除可能）
aws ec2 delete-network-interface --network-interface-id eni-xxxxx
```

### Lambda VPC外 → VPC内への移行

ECSの追加などでVPC内にLambdaを戻す必要がある場合：

1. `vpc.tf` のコメントアウトを解除:
   - NAT Gateway
   - Elastic IP
   - VPC Endpoints
   - Lambda Security Group

2. `lambda.tf` の `vpc_config` ブロックを復活

3. `iam.tf` の `lambda_vpc_access` ポリシーアタッチメントを復活

## ファイル構成

```
terraform/
├── main.tf          # プロバイダー設定、ローカル変数
├── variables.tf     # 入力変数定義
├── outputs.tf       # 出力値定義
├── vpc.tf           # VPC、サブネット、SG（ECS用に保持）
├── dynamodb.tf      # DynamoDBテーブル
├── lambda.tf        # Lambda関数 x 9
├── step_functions.tf # Step Functions ワークフロー
├── eventbridge.tf   # EventBridge スケジュール
├── s3.tf            # S3バケット
├── sqs.tf           # SQSキュー
├── sns.tf           # SNS + Slack通知Lambda
├── iam.tf           # IAMロール・ポリシー
└── terraform.tfvars # 変数値（gitignore推奨）
```

## 将来の拡張

### ECS追加時

`enable_ecs = true` を設定すると、以下が有効化されます：
- ALB Security Group
- ECS Security Group
- （別途ecs.tfの作成が必要）

この場合、NAT Gatewayの復活も検討してください（VPC内のコンテナがインターネットにアクセスするため）。

## トラブルシューティング

### "DependencyViolation: Security Group is in use" エラー

Lambda ENIがまだ存在している場合に発生。上記「VPC移行ガイド」を参照。

### Lambda実行エラー

```bash
# テスト実行
aws lambda invoke --function-name eth-trading-price-collector --payload '{}' response.json

# ログ確認
aws logs tail /aws/lambda/eth-trading-price-collector --follow
```
