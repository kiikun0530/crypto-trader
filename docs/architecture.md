# アーキテクチャ設計書

Crypto Trader のシステム構成と技術選定を説明するドキュメントです。

- **売買戦略・ロジック**: [trading-strategy.md](trading-strategy.md)
- **Lambda関数リファレンス**: [lambda-reference.md](lambda-reference.md)

---

## システム構成図

> **推定コスト**: AWS 約$6/月 + CryptoPanic Growth $199/月（オプション）
> Lambda VPC外実行により NAT Gateway ($45/月) を削減

```mermaid
flowchart LR
    subgraph External["External APIs"]
        API_BINANCE["Binance API<br/>6通貨の価格取得"]
        API_COINCHECK["Coincheck API<br/>取引執行（JPYペア）"]
        API_CRYPTOPANIC["CryptoPanic API v2<br/>Growth Plan"]
        API_FNG["Alternative.me<br/>Fear & Greed Index"]
        API_FUNDING["Binance Futures<br/>ファンディングレート"]
        API_COINGECKO["CoinGecko Global<br/>BTC Dominance"]
        SLACK["Slack Webhook"]
    end

    subgraph EventBridge["EventBridge Scheduler"]
        EB_PRICE["5分間隔<br/>price-collection"]
        EB_POSITION["5分間隔<br/>position-monitor"]
        EB_NEWS["30分間隔<br/>news-collection"]
        EB_MKTCTX["30分間隔<br/>market-context"]
        EB_DAILY["毎日23:00 JST<br/>daily-reporter"]
    end

    subgraph Lambda["Lambda Functions (VPC外)"]
        L_PRICE["price-collector<br/>全6通貨を収集"]
        L_TECH["technical<br/>テクニカル分析"]
        L_CHRONOS["chronos-caller<br/>AI価格予測"]
        L_SENTIMENT["sentiment-getter<br/>センチメント取得"]
        L_AGG["aggregator<br/>全通貨スコアリング"]
        L_ORDER["order-executor<br/>注文実行"]
        L_POSITION["position-monitor<br/>全通貨SL/TP監視"]
        L_NEWS["news-collector<br/>ニュース収集"]
        L_MKTCTX["market-context<br/>市場環境収集"]
        L_REMEDIATE["error-remediator<br/>エラー自動修復"]
        L_DAILY["daily-reporter<br/>日次レポート+自動改善"]
    end

    subgraph StepFunctions["Step Functions (Map State)"]
        SF_MAP["Map: 全通貨をイテレート"]
        SF_PARALLEL["Parallel: 3分析を並列実行"]
        SF_AGG["Aggregator: 全通貨スコア比較<br/>+ Market Context取得"]
    end

    subgraph Messaging["Messaging"]
        SQS_ORDER[["order-queue"]]
        SQS_DLQ[["order-dlq"]]
        SNS_ALERTS{{"alerts"}}
    end

    subgraph DynamoDB["DynamoDB (7 Tables, 全通貨共有)"]
        DB_PRICES[("prices<br/>pair=PK, TTL:14日")]
        DB_SENTIMENT[("sentiment<br/>pair=PK, TTL:14日")]
        DB_POSITIONS[("positions<br/>pair=PK")]
        DB_TRADES[("trades<br/>pair=PK")]
        DB_SIGNALS[("signals<br/>pair=PK, TTL:90日")]
        DB_STATE[("analysis_state<br/>pair=PK")]
        DB_MKTCTX[("market-context<br/>context_type=PK, TTL:14日")]
    end

    subgraph SageMaker["SageMaker"]
        SM_CHRONOS["eth-trading-chronos-base<br/>Serverless Endpoint<br/>Chronos-2 (120M)<br/>MaxConcurrency=8"]
    end

    subgraph S3["S3"]
        S3_MODEL["chronos-2/<br/>model.tar.gz"]
    end

    subgraph Monitoring["Monitoring"]
        CW_LOGS["CloudWatch Logs"]
        CW_ALARM["CloudWatch Alarms<br/>全Lambda監視<br/>(Errors + Duration)"]
        CW_FILTER["Subscription Filters<br/>エラーログ検知"]
    end

    %% 定期実行
    EB_PRICE -->|"毎5分"| L_PRICE
    EB_POSITION -->|"毎5分"| L_POSITION
    EB_NEWS -->|"30分毎"| L_NEWS
    EB_MKTCTX -->|"30分毎"| L_MKTCTX

    %% 価格収集 → 分析トリガー
    L_PRICE -->|"6通貨取得"| API_BINANCE
    L_PRICE -->|"保存"| DB_PRICES
    L_PRICE -->|"変動検知"| SF_MAP

    %% Step Functions (Map)
    SF_MAP -->|"通貨ごと"| SF_PARALLEL
    SF_PARALLEL --> L_TECH
    SF_PARALLEL --> L_CHRONOS
    SF_PARALLEL --> L_SENTIMENT
    L_TECH --> SF_AGG
    L_CHRONOS --> SF_AGG
    L_SENTIMENT --> SF_AGG
    SF_AGG --> L_AGG

    %% DynamoDB連携
    L_TECH -->|"R"| DB_PRICES
    L_CHRONOS -->|"R"| DB_PRICES
    L_CHRONOS -->|"SageMaker Endpoint"| SM_CHRONOS
    L_SENTIMENT -->|"R"| DB_SENTIMENT
    L_NEWS -->|"W"| DB_SENTIMENT
    L_MKTCTX -->|"W"| DB_MKTCTX
    L_PRICE -->|"R/W"| DB_STATE
    L_AGG -->|"R"| DB_POSITIONS
    L_AGG -->|"R"| DB_MKTCTX
    L_AGG -->|"W"| DB_SIGNALS
    L_ORDER -->|"R/W"| DB_POSITIONS
    L_ORDER -->|"W"| DB_TRADES
    L_POSITION -->|"R"| DB_POSITIONS

    %% 注文
    L_AGG -->|"最高スコア通貨"| SQS_ORDER
    SQS_ORDER --> L_ORDER
    SQS_ORDER -.->|"3回失敗"| SQS_DLQ

    %% ポジション監視
    L_POSITION -->|"全通貨チェック"| API_COINCHECK

    %% 通知（直接Slack Webhook）
    L_ORDER -->|"Slack通知"| SLACK
    L_AGG -->|"ランキング通知"| SLACK
    L_POSITION -->|"SL/TP通知"| SLACK

    %% 外部API
    L_ORDER --> API_COINCHECK
    L_NEWS --> API_CRYPTOPANIC
    L_MKTCTX --> API_FNG
    L_MKTCTX --> API_FUNDING
    L_MKTCTX --> API_COINGECKO

    %% 監視・自動修復
    SQS_DLQ -.->|"滞留監視"| CW_ALARM
    CW_ALARM -->|"アラート"| SNS_ALERTS
    SNS_ALERTS --> SLACK
    CW_LOGS -->|"エラーパターン"| CW_FILTER
    CW_FILTER --> L_REMEDIATE
    L_REMEDIATE -->|"Slack通知"| SLACK

```

---

## 対応通貨

| 分析ペア (Binance) | 取引ペア (Coincheck) | CryptoPanic | 通貨名 |
|---|---|---|---|
| ETHUSDT | eth_jpy | ETH | Ethereum |
| BTCUSDT | btc_jpy | BTC | Bitcoin |
| XRPUSDT | xrp_jpy | XRP | XRP |
| SOLUSDT | sol_jpy | SOL | Solana |
| DOGEUSDT | doge_jpy | DOGE | Dogecoin |
| AVAXUSDT | avax_jpy | AVAX | Avalanche |

### なぜこの6通貨か

**選定基準**: Binance（分析用）と **Coincheck取引所**（取引用）の **両方で扱える** + **流動性が高い** + **取引所手数料0%** の通貨

- 日本の法規制上、取引は金融庁登録業者（Coincheck）で行う必要がある
- Coincheck「販売所」はスプレッドが大きいため、**取引所**で売買可能な通貨のみ選定
- テクニカル分析用のOHLCデータは Binance の方が高品質かつ無料
- 6通貨は分析コスト（Lambda 18回/分析）と網羅性のバランスが良い
- 10通貨以上にすると CryptoPanic API レスポンスが肥大化し、Lambda実行時間が増加
- `TRADING_PAIRS_CONFIG` 環境変数で通貨の追加・削除が可能（コード変更不要）

**参考 (Coincheck)**:
- [取引所手数料](https://coincheck.com/ja/exchange/fee) — 通貨別 Maker/Taker 手数料率
- [取引所 API](https://coincheck.com/ja/documents/exchange/api) — 利用可能な通貨ペア一覧、注文 API 仕様
- [取引注文ルール](https://faq.coincheck.com/s/article/40218?language=ja) — 最小注文数量・小数点以下桁数

---

## 設計原則

### 1. コスト最小化 — 月額 $10 以下

暗号通貨トレーディングボットは24時間365日稼働が必要だが、常にCPUリソースを使う必要はない。「イベント駆動 + Serverless」で、実際に処理が必要な時だけコストが発生する構成にしている。

### 2. 信頼性 — 注文の確実な実行

金融取引では「注文を出したつもりが出ていなかった」が最も危険。SQS + DLQ で失敗した注文の追跡と再試行を保証。DLQ 滞留時は CloudWatch Alarm → Slack で即座に人間に通知。

### 3. シンプルさ — 運用負荷ゼロ

EC2 や ECS のようなサーバー管理は行わず、全てマネージドサービスで構成。パッチ適用、スケーリング、ログローテーションなどの運用作業が不要。

### 4. 拡張性 — 通貨追加がコード変更不要

DynamoDB は全テーブルが `pair` を Partition Key にしており、通貨追加はデータ層の変更不要。`TRADING_PAIRS_CONFIG` 環境変数を変更するだけで対応通貨を増減できる。

---

## 技術選定の理由

### Lambda vs EC2 vs ECS

| 選択肢 | メリット | デメリット | 採用 |
|---|---|---|---|
| EC2 | 柔軟性が高い | 常時課金、運用負荷 | ❌ |
| ECS Fargate | コンテナ実行 | 常時課金（最低$15/月） | ❌ |
| Lambda | 実行時のみ課金 | 15分制限、コールドスタート | ✅ |

- 各処理は数秒～数十秒で完了するため、15分制限は問題なし
- コールドスタートは許容範囲（数百ms、取引に影響なし）

### AI価格予測 (Chronos) のインフラ選定

スコアリング全体の **25%のウェイト** を占める AI 価格予測コンポーネントについて、以下の選択肢を比較検討した。

| 選択肢 | 方式 | 月額 | 推論時間 | 精度 | 運用負荷 |
|---|---|---|---|---|---|
| モメンタム代替 | Lambda 内計算 | $0 | <1秒 | ❌ 予測ではない | なし |
| Lambda + ONNX | Chronos-Tiny ONNX変換 | ~$0 | 3-10秒 | ⭕ | 中 |
| **SageMaker Serverless** | **Chronos-2 (120M)** | **~$3-8** | **2-5秒** | **◎** | **低（クォータ申請済）** |
| SageMaker Real-time | Chronos-Small (46M) | ~$50-80 | 1-3秒 | ◎ | 低 |
| ECS Fargate Spot | Chronos-Small コンテナ | ~$15-25 | 2-5秒 | ◎ | 中 |
| EC2 Spot GPU | Chronos-Large (710M) | ~$25-60 | <1秒 | ◎◎ | 高 |

**選定: SageMaker Serverless Endpoint（Chronos-2）**

初期は Lambda + ONNX Runtime (Chronos-Tiny 8M) → Chronos-T5-Base (200M) → 現在 Chronos-2 (120M) へ移行:
- **モデル更新**: T5-Base (200M) → Chronos-2 (120M) — 120Mパラメータでも250倍高速、10%高精度
- **推論方式**: 50回サンプリング → 分位数直接出力 (q10/q50/q90) — サンプリング不要で大幅高速化
- **入力データ量**: 336本 (28h) で日次サイクルのパターン認識強化
- **サーバーレス維持**: 推論リクエスト時のみ課金、アイドル時は0円
- **フォールバック**: SageMaker障害時はモメンタムベースの代替スコアに自動切替
- **デプロイスクリプト**: `scripts/deploy_sagemaker_chronos.py` で再デプロイ可能

#### SageMaker Serverless クォータ

| クォータ | 値 | 備考 |
|----------|-----|------|
| アカウント全体の最大同時実行数 | 10 | AWS Service Quotas で承認済 |
| エンドポイントの MaxConcurrency | 8 | 6通貨並列 + マージン2 |
| Step Functions MaxConcurrency | 6 | 6通貨ペアの並列分析 |

⚠️ **注意**: AWS クォータ（10）は「全Serverlessエンドポイントの MaxConcurrency 合計値の上限」であり、実際のエンドポイントの `MaxConcurrency` は別途設定が必要。

ECS/EC2 は常時課金が発生し、現行の「完全サーバーレス」設計思想に反する。

### VPC外実行

**削減コスト**: NAT Gateway $45/月 + Elastic IP $3.6/月 = **$48.6/月**

Lambda を VPC 内に配置すると、外部 API（Binance, Coincheck, CryptoPanic）へのアクセスに NAT Gateway が必須。しかし DynamoDB, SQS, SNS 等のAWSサービスは IAM 認証でアクセスでき、VPC内にある必要がない。Coincheck の API キーは Secrets Manager（IAMロール保護）で管理。

### Binance（分析） + Coincheck（取引）

| API | 価格データ | 取引 | 用途 |
|---|---|---|---|
| Binance | ✅ 5分足OHLC、無料 | ❌ 日本居住者不可 | 価格取得・分析 |
| Coincheck | ⚠️ 現在価格のみ | ✅ 日本円取引可 | 取引執行 |

テクニカル分析にはOHLC（始値・高値・安値・終値）が必要だが、CoincheckはOHLCを提供していない。

### 5分間隔

| 間隔 | 実行回数/日 | 月額概算 |
|---|---|---|
| 1分 | 1,440 | ~$3 |
| **5分** | **288** | **~$0.6** |
| 15分 | 96 | ~$0.2 |

- SMA200 に必要な最低データ量（約17時間分）を 5分足で十分カバー
- 急変時は変動閾値（0.3%）を超えた時点で即座に分析開始
- 1分間隔は暗号通貨のボラティリティに対して過剰

### Step Functions (Map State)

```
price-collector
  └→ Step Functions
       └→ Map: [eth_usdt, btc_usdt, xrp_usdt, sol_usdt, doge_usdt, avax_usdt]
            MaxConcurrency = 6 (SageMaker Serverless MaxConcurrency=8 の範囲内)
            └→ Parallel: [テクニカル分析, AI予測, センチメント取得]
       └→ Aggregator: 全通貨のスコアを比較 → 最高期待値の通貨を選定
```

- Map State で全通貨を **並列分析** → 6通貨×3分析 = 18 Lambda が並列実行
- `MaxConcurrency = 6` で SageMaker Serverless のスロットリング防止
- ワークフローの可視化・リトライ・エラーハンドリングを Step Functions が提供
- Lambda の直接連鎖だと失敗時の状態管理が複雑

### SQS + DLQ

```
aggregator → SQS(order-queue) → order-executor
                    ↓ (3回失敗)
              SQS(order-dlq) → CloudWatch Alarm → Slack通知
```

注文は絶対に失落させてはいけない。SQS は自動リトライ（3回）を提供し、DLQ で失敗した注文を捕捉して即座に通知。

### 監視・自動修復パイプライン

全 Lambda に CloudWatch Metric Alarms（Errors + Duration）を設定し、異常検知時は即座に Slack 通知。さらに、エラーログを自動検知して Claude AI が修正コードを生成・デプロイする自動修復パイプラインを構築。

```
CloudWatch Logs → Subscription Filter → error-remediator Lambda
                                            └→ Slack通知（エラー内容）
```

- **CloudWatch Alarms (24個)**: 全12 Lambda × (Errors + Duration) で異常検知
- **Subscription Filters (11個)**: warm-up以外の全Lambdaのエラーログを検知
  - フィルターパターン: `?"[ERROR]" ?Traceback ?"raise Exception" -"[INFO]" -"expected behavior" -"retrying in"`
  - SageMaker Serverless の想定内リトライログ（ThrottlingException → 自動リカバリ）を除外
- **error-remediator Lambda**: エラー検知 → Slack通知（30分クールダウン付き）

### 自動改善パイプライン (Phase 4)

```
EventBridge (23:00 JST) → daily-reporter Lambda
                              ├→ S3にJSON日次レポート保存 (90日保持)
                              └→ Slackに日次サマリー通知
```

- **安全制約**: ウェイト±0.05/回、閾値±0.03/回、2週間以内の再変更抑止
- **データ品質ゲート**: Wilson信頼区間95%、最低3トレード、クールダウン2週間
- **コスト**: ~$0.01/日 (Claude API + Lambda)

### DynamoDB

| 選択肢 | メリット | デメリット | 採用 |
|---|---|---|---|
| RDS (PostgreSQL) | 柔軟なクエリ | 常時課金、VPC必須 | ❌ |
| Aurora Serverless | スケーラブル | 最低コスト高い | ❌ |
| DynamoDB | オンデマンド課金、TTL | NoSQLの制約 | ✅ |

- 全テーブルが `pair + timestamp` のシンプルなキー構造
- 複雑な JOIN やトランザクションは不要
- オンデマンドモードで使った分だけ課金
- TTL で古いデータを自動削除（ストレージコスト削減）

---

## DynamoDB テーブル設計

| テーブル | PK | SK | TTL | 用途 |
|---|---|---|---|---|
| prices | pair (S) | timestamp (N) | 14日 | 全通貨の価格履歴 |
| sentiment | pair (S) | timestamp (N) | 14日 | 通貨別センチメントスコア |
| signals | pair (S) | timestamp (N) | 90日 | 分析シグナル履歴 |
| positions | pair (S) | position_id (S) | - | ポジション管理 |
| trades | pair (S) | timestamp (N) | 90日 | 取引履歴 |
| analysis_state | pair (S) | - | - | 通貨別の最終分析時刻 |
| market-context | context_type (S) | timestamp (N) | 14日 | マクロ市場環境指標 |
| improvements | improvement_id (S) | - | 180日 | 自動改善履歴 (Phase 4) |

### TTL 設計の根拠

| テーブル | TTL | 理由 |
|---|---|---|
| prices | 14日 | SMA200 に必要な最低データ量を保持 |
| sentiment | 14日 | ニュース相関分析に2週間分必要 |
| signals | 90日 | パフォーマンス分析用に長めに保持 |
| positions | なし | 取引履歴は永続保存（税務対応） |
| trades | 90日 | 自動クリーンアップ (Phase 4で追加) |
| market-context | 14日 | マクロ指標は短期分のみ必要 |
| improvements | 180日 | 自動改善の効果追跡用 |

---

## セキュリティ設計

| 認証情報 | 保存先 | 理由 |
|---|---|---|
| AWS認証 | IAMロール | Lambda実行ロールで自動付与 |
| Coincheck API | Secrets Manager | 取引に直結するため厳重管理 |
| GitHub PAT | Secrets Manager | 自動修復パイプライン用（repo権限） |
| Anthropic API | GitHub Secrets | 自動修復パイプライン用 |
| CryptoPanic API | Lambda環境変数 | 読み取り専用、リスク低 |
| Slack Webhook | Lambda環境変数 | 読み取り専用、リスク低 |

IAM ロールは最小権限原則で設計。各 Lambda は必要な DynamoDB テーブル・SQS・SNS のみアクセス可能。

---

## コスト内訳

### AWS費用（6通貨構成）

| 項目 | 月額 | 備考 |
|---|---|---|
| Lambda | ~$5.00 | 6通貨分析 + ONNX推論 + error-remediator + daily-reporter含む |
| DynamoDB | ~$0.35 | 8テーブル×6通貨分のR/W + クールダウン + improvements |
| Step Functions | ~$0.10 | Map State で遷移数増加 |
| CloudWatch | ~$0.55 | ログ保存14日 + Metric Alarms 20個 + Subscription Filters |
| Secrets Manager | ~$0.50 | Coincheck + GitHub PAT |
| SQS/SNS/EventBridge | ~$0.05 | 軽微 |
| **AWS合計** | **~$7/月** | |

### 外部API費用

| API | 費用 | 備考 |
|---|---|---|
| Binance | 無料 | 6通貨分の価格取得 + ファンディングレート（認証不要） |
| Alternative.me | 無料 | Fear & Greed Index |
| CoinGecko | 無料 | BTC Dominance |
| CryptoPanic | 無料 or $199/月 | Growth Plan でリアルタイム取得 |
| Coincheck | 0% | 取引手数料無料 |
| Anthropic | 従量制 | 自動修復パイプライン (~$0.01-0.03/修復) |

### 総コスト

| 構成 | 月額 |
|---|---|
| 無料プラン | **~$7/月** |
| Growth Plan | **~$206/月** |

---

## 関連ドキュメント

- [trading-strategy.md](trading-strategy.md) — マルチ通貨選定ロジック、スコアリング、売買判定
- [lambda-reference.md](lambda-reference.md) — 各Lambda関数の仕様、I/O、設定
