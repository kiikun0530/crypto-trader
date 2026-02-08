# Crypto Trader

AWS Serverless で構築したマルチ通貨対応の暗号通貨自動売買システム

## 概要

6通貨（ETH / BTC / XRP / DOT / LINK / AVAX）を同時に分析し、最も期待値の高い通貨を自動で選択・売買する。

- **対象通貨**: 6通貨（Binance + Coincheck 両対応の銘柄を厳選）
- **価格データ**: Binance API（5分足 OHLC × 6通貨）
- **取引執行**: Coincheck API（各通貨/JPY）
- **テクニカル分析**: SMA20/200、RSI、MACD、ボリンジャーバンド
- **ニュースセンチメント**: CryptoPanic API（全通貨一括取得 + BTC相関分析）
- **時系列予測**: Amazon Chronos（ECS未デプロイ時はモメンタム代替）
- **ポジション管理**: 1通貨のみ保有（他通貨排他制御）
- **通知**: Slack Webhook（全通貨ランキング付き）

## アーキテクチャ

- **設計ドキュメント**:
  - [docs/architecture.md](docs/architecture.md) — システム構成・設計思想
  - [docs/trading-strategy.md](docs/trading-strategy.md) — 売買戦略・スコアリング
  - [docs/lambda-reference.md](docs/lambda-reference.md) — Lambda関数リファレンス

> GitHub上でMermaidダイアグラムがレンダリングされます

### 対象通貨（6通貨）

| 通貨 | Binanceペア | Coincheckペア | 選定理由 |
|------|------------|--------------|----------|
| ETH | ETHUSDT | eth_jpy | DeFi基盤、高流動性 |
| BTC | BTCUSDT | btc_jpy | 市場牽引力、Coincheck対応 |
| XRP | XRPUSDT | xrp_jpy | 送金特化、高速決済 |
| DOT | DOTUSDT | dot_jpy | クロスチェーン基盤 |
| LINK | LINKUSDT | link_jpy | オラクル需要 |
| AVAX | AVAXUSDT | avax_jpy | 高速L1、DeFi成長 |

### Lambda関数（9個）

| 関数名 | 役割 | 実行間隔 |
|--------|------|----------|
| price-collector | 全6通貨の価格取得・変動検知 | 5分 |
| technical | テクニカル指標計算（RSI, MACD, SMA, BB） | Step Functions (×6) |
| chronos-caller | AI時系列予測 / モメンタム代替 | Step Functions (×6) |
| sentiment-getter | 通貨別センチメントスコア取得 | Step Functions (×6) |
| aggregator | 全通貨スコアリング・ランキング・売買判定 | Step Functions |
| order-executor | Coincheckで成行注文実行（1ポジション制御） | SQSトリガー |
| position-monitor | 全通貨のSL(-5%)/TP(+10%)監視 | 5分 |
| news-collector | 全通貨ニュース一括取得・BTC相関分析 | 30分 |
| warm-up | 全通貨の初回データ投入（手動） | - |

### DynamoDBテーブル（6個）

| テーブル | TTL | 用途 |
|----------|-----|------|
| prices | 14日 | 価格履歴 |
| sentiment | 14日 | センチメントスコア |
| signals | 90日 | 売買シグナル履歴 |
| positions | - | ポジション管理 |
| trades | - | 取引履歴 |
| analysis_state | - | 分析状態管理 |

## 推定コスト

### AWSインフラ費用（6通貨分析時）

| 項目 | 月額 |
|------|------|
| Lambda | ~$5.00 |
| DynamoDB | ~$0.25 |
| Step Functions | ~$0.10 |
| CloudWatch | ~$0.10 |
| Secrets Manager | ~$0.50 |
| **合計** | **~$6** |

> 詳細な計算式は [docs/architecture.md](docs/architecture.md) を参照

### 外部API費用

| API | 費用 | 備考 |
|-----|------|------|
| Binance | 無料 | 価格データ取得のみ（認証不要） |
| CryptoPanic | 無料 or $199/月 | Growth Planでリアルタイムニュース取得 |
| Coincheck | 0% | 取引所取引は手数料無料 |

> **総コスト目安**: 無料構成 ~$6/月、Growth Plan ~$205/月

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
max_position_jpy     = 100000     # 最大ポジション（円）
slack_webhook_url    = "https://hooks.slack.com/services/xxx/xxx/xxx"
cryptopanic_api_key  = ""         # オプション
```

### 7. Terraformでデプロイ

```bash
terraform init
terraform plan
terraform apply
```

### 8. 初回データ投入

```bash
# 全6通貨の過去1000件の価格データを一括投入
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

## スコアベースの投資ロジック

シグナルスコアに応じて投資金額が自動調整されます：

| スコア | 投資比率 | 説明 |
|--------|----------|------|
| 0.90+ | 100% | 非常に強気 |
| 0.80-0.90 | 75% | 強気 |
| 0.70-0.80 | 50% | やや強気 |
| 0.65-0.70 | 30% | 弱気 |
| 0.65未満 | 0% | 注文なし |

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
│   └── warm-up/
├── docs/
│   ├── architecture.md     # システム構成・設計思想
│   ├── trading-strategy.md # 売買戦略・スコアリング
│   └── lambda-reference.md # Lambda関数リファレンス
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