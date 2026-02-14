# Crypto Trader

AWS Serverless で構築したマルチ通貨対応の暗号通貨自動売買システム

## 概要

3通貨（BTC / ETH / XRP）を同時に分析し、最も期待値の高い通貨を自動で選択・売買する。

- **対象通貨**: 3通貨（Binance + Coincheck 両対応の高流動性銘柄を厳選）
- **価格データ**: Binance API（マルチタイムフレーム OHLCV × 3通貨 × 4TF）
- **取引執行**: Coincheck API（各通貨/JPY）
- **テクニカル分析**: SMA20/200、RSI、MACD、ボリンジャーバンド、ADX、ATR
- **レジーム検知**: ADXによるトレンド/レンジ判定、適応型ウェイト
- **ニュースセンチメント**: CryptoPanic API（全通貨一括取得 + BTC相関分析）
- **時系列予測**: Amazon Chronos-2 (120M) on SageMaker Serverless Endpoint
- **ポジション管理**: 複数通貨同時保有対応（SELL優先 → 未保有通貨をBUY）
- **リスク管理**: SL/TP + トレーリングストップ + サーキットブレーカー
- **通知**: Slack Webhook（全通貨ランキング付き）

## アーキテクチャ

- **設計ドキュメント**:
  - [docs/architecture.md](docs/architecture.md) — システム構成・設計思想
  - [docs/trading-strategy.md](docs/trading-strategy.md) — 売買戦略・スコアリング
  - [docs/lambda-reference.md](docs/lambda-reference.md) — Lambda関数リファレンス
  - [docs/bugfix-history.md](docs/bugfix-history.md) — バグ修正履歴・設計原則
  - [docs/improvement-issues.md](docs/improvement-issues.md) — 改善課題

> GitHub上でMermaidダイアグラムがレンダリングされます

### 対象通貨（3通貨）

| 通貨 | Binanceペア | Coincheckペア | 選定理由 |
|------|------------|--------------|----------|
| BTC | BTCUSDT | btc_jpy | 市場牽引力、最高流動性 |
| ETH | ETHUSDT | eth_jpy | DeFi基盤、高流動性 |
| XRP | XRPUSDT | xrp_jpy | 送金特化、高速決済 |

### Lambda関数（11個）

| 関数名 | 役割 | 実行間隔 |
|--------|------|----------|
| price-collector | TF別全通貨の価格収集 | Step Functions (Phase 1) |
| technical | テクニカル指標計算（RSI, MACD, SMA, BB, ADX, ATR） | Step Functions (×3) |
| chronos-caller | AI時系列予測 (SageMaker Serverless, Chronos-2 120M) | Step Functions (×3) |
| sentiment-getter | 通貨別センチメントスコア取得 | Step Functions (×3) |
| aggregator | TFスコア保存 / メタ集約・ランキング・売買判定（デュアルモード） | Step Functions / EventBridge 15分 |
| order-executor | Coincheckで成行注文実行（同一通貨重複防止） | EventBridge 15分 |
| position-monitor | 全通貨のSL(-5%)/TP(+30%)/トレーリングストップ監視 | 5分 |
| news-collector | 全通貨ニュース一括取得・BTC相関分析 | 30分 |
| market-context | F&G / Funding Rate / BTC Dominance 収集 | 30分 |
| error-remediator | Lambdaエラー検知→Slack通知 | CloudWatch Logs |
| warm-up | 全通貨の初回データ投入（手動） | - |

### DynamoDBテーブル（8個）

| テーブル | TTL | 用途 |
|----------|-----|------|
| prices | TF別 (14d-365d) | 全通貨×全TFの価格履歴 |
| tf-scores | 24時間 | TF別スコア保存 |
| sentiment | 14日 | センチメントスコア |
| signals | 90日 | 売買シグナル履歴 |
| positions | - | ポジション管理 |
| trades | 90日 | 取引履歴 |
| analysis_state | - | 分析状態管理 |
| market-context | 14日 | マクロ市場環境指標 |

## 推定コスト

### AWSインフラ費用（3通貨 × 4TF分析時）

| 項目 | 月額 |
|------|------|
| Lambda | ~$4.00 |
| DynamoDB | ~$0.30 |
| Bedrock (Amazon Nova Micro) | ~$2.00 |
| SageMaker Serverless (Chronos-2) | ~$3-8 |
| Step Functions | ~$0.15 |
| CloudWatch | ~$0.55 |
| Secrets Manager | ~$0.50 |
| SNS/EventBridge | ~$0.05 |
| **合計** | **~$11/月** |

> 詳細な計算式は [docs/architecture.md](docs/architecture.md) を参照

### 外部API費用

| API | 費用 | 備考 |
|-----|------|------|
| Binance | 無料 | 価格データ + ファンディングレート取得（認証不要） |
| Alternative.me / CoinGecko | 無料 | F&G Index / BTC Dominance |
| CryptoPanic | 無料 or $199/月 | Growth Planでリアルタイムニュース取得 |
| Coincheck | 0% | 取引所取引は手数料無料 |

> **総コスト目安**: 無料構成 ~$11/月、Growth Plan ~$210/月

## 前提条件

- AWS アカウント
- Terraform v1.0+
- Python 3.11+
- Coincheck アカウント（本人確認済み）
- Slack ワークスペース

### 💰 Coincheck入金について

| 項目 | 値 |
|------|-----|
| 最低取引量 | 通貨により異なる（例: ETH 0.001, BTC 0.001） |
| システム最低注文額 | 500円（MIN_ORDER_JPY） |
| 推奨入金額 | 10,000円〜 |

> ⚠️ **注意**: 日本円残高が500円未満だと注文が実行されません。  
> 余裕を持って1万円以上の入金を推奨します。

## セットアップ手順

### 1. リポジトリをクローン

```bash
git clone https://github.com/kiikun0530/crypto-trader.git
cd crypto-trader
```

### 2. AWS認証設定

```bash
# AWS CLIの設定
aws configure
# または環境変数で設定
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=ap-northeast-1
```

### 3. Coincheck APIキーを Secrets Manager に登録

```bash
aws secretsmanager create-secret \
  --name coincheck/api-credentials \
  --secret-string '{"access_key":"YOUR_ACCESS_KEY","secret_key":"YOUR_SECRET_KEY"}'
```

> ⚠️ Coincheck APIキーには「取引」権限が必要です

### 4. Slack Webhook URL を取得

1. https://api.slack.com/apps にアクセス
2. 「Create New App」→「From scratch」
3. 「Incoming Webhooks」を有効化
4. チャンネルを選択してWebhook URLを取得

### 5. CryptoPanic APIキー（オプション）

1. https://cryptopanic.com/developers/api/ にアクセス
2. アカウント作成後、APIキーを取得
3. Growth Plan（$199/月）でリアルタイムニュース取得可能

### 6. Terraform変数を設定

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` を編集：

```hcl
environment          = "prod"
aws_region           = "ap-northeast-1"
volatility_threshold = 0.3        # 価格変動閾値（%）
max_position_jpy     = 15000      # 最大ポジション（円）
slack_webhook_url    = "https://hooks.slack.com/services/xxx/xxx/xxx"
cryptopanic_api_key  = ""         # オプション
```

### 7. Terraformでデプロイ

```bash
terraform init
terraform plan
terraform apply
```

### 8. SageMaker Chronos-2 のデプロイ

```bash
# Chronos-2 モデルを SageMaker Serverless Endpoint にデプロイ
python scripts/deploy_sagemaker_chronos.py
```

> 初回デプロイ時のみ必要です。SageMaker Serverless のクォータ申請（MaxConcurrency上限）が事前に必要な場合があります。

### 9. 初回データ投入

```bash
# 全3通貨の過去データを一括投入
aws lambda invoke \
  --function-name eth-trading-warm-up \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  response.json

# 特定の通貨のみ投入する場合
aws lambda invoke \
  --function-name eth-trading-warm-up \
  --payload '{"pair": "btc_usdt"}' \
  --cli-binary-format raw-in-base64-out \
  response.json
```

## 動作確認

```bash
# price-collector を手動実行
aws lambda invoke \
  --function-name eth-trading-price-collector \
  --payload '{}' \
  response.json

# DynamoDBの価格データを確認
aws dynamodb scan \
  --table-name eth-trading-prices \
  --limit 5

# CloudWatch Logsを確認
aws logs tail /aws/lambda/eth-trading-price-collector --since 5m
```

## 監視・通知

### CloudWatch 監視

- **Metric Alarms (20個)**: 全10 Lambda × (Errors + Duration)
- **Subscription Filters (9個)**: warm-up以外の全Lambdaのエラーログをリアルタイム検知
- 異常検知時は即座に Slack 通知

### エラー検知パイプライン

```
CloudWatch Logs → Subscription Filter → error-remediator Lambda
                                            └→ Slack通知（エラー内容）
```

- エラー検知からSlack通知まで自動化
- 同一関数のエラーは30分間隔でクールダウン

## スコアベースの投資ロジック

シグナルスコアに応じて投資金額が自動調整されます（Kelly Criterion 適用、トレード履歴5件未満時はフォールバック比率）：

| スコア | フォールバック投資比率 | 説明 |
|--------|----------|------|
| 0.45+ | 60% | 非常に強いシグナル |
| 0.35-0.45 | 45% | 強いシグナル |
| 0.25-0.35 | 30% | 中程度のシグナル |
| 0.15-0.25 | 20% | 弱いシグナル |
| 0.15未満 | 0% | 見送り |

## 手数料

Coincheck取引所の取引手数料は **0%** です（2026年2月時点、対象通貨: ETH, BTC, XRP等）。

## ローカル開発

### Python環境構築

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Lambda関数のテスト

```bash
cd services/price-collector
python -c "from handler import handler; print(handler({}, None))"
```

## プロジェクト構造

```
crypto-trader/
├── terraform/           # Terraform IaC
│   ├── main.tf
│   ├── lambda.tf
│   ├── dynamodb.tf
│   ├── eventbridge.tf
│   ├── stepfunctions.tf
│   ├── monitoring.tf    # CloudWatch Alarms + Subscription Filters
│   └── ...
├── services/            # Lambda関数
│   ├── price-collector/
│   ├── technical/
│   ├── chronos-caller/
│   ├── sentiment-getter/
│   ├── aggregator/
│   ├── order-executor/
│   ├── position-monitor/
│   ├── news-collector/
│   ├── market-context/
│   ├── error-remediator/
│   └── warm-up/
├── scripts/
│   ├── convert_chronos_onnx.py  # ONNX変換スクリプト（レガシー）
│   └── deploy_sagemaker_chronos.py  # SageMaker Chronos-2 デプロイスクリプト
├── models/
├── docs/
│   ├── architecture.md     # システム構成・設計思想
│   ├── trading-strategy.md # 売買戦略・スコアリング
    ├── lambda-reference.md # Lambda関数リファレンス
    ├── bugfix-history.md       # バグ修正履歴
    └── improvement-issues.md   # 改善課題
└── README.md
```

## リソース削除

```bash
cd terraform
terraform destroy
```

> ⚠️ DynamoDBのデータも削除されます

## 免責事項

- このシステムは教育・研究目的で作成されています
- 実際の取引による損失について、作者は一切責任を負いません
- 必ず少額でテストしてから本番運用してください
- 暗号通貨取引にはリスクが伴います

## ライセンス

MIT License