# Lambda関数リファレンス

全12個の Lambda 関数の仕様、入出力、設定の詳細。

---

## 全Lambda共通

### 共通環境変数

| 変数名 | 用途 |
|---|---|
| `PRICES_TABLE` | 価格テーブル名 |
| `SENTIMENT_TABLE` | センチメントテーブル名 |
| `POSITIONS_TABLE` | ポジションテーブル名 |
| `TRADES_TABLE` | 取引テーブル名 |
| `SIGNALS_TABLE` | シグナルテーブル名 |
| `ANALYSIS_STATE_TABLE` | 分析状態テーブル名 |
| `COINCHECK_SECRET_ARN` | Coincheck API認証情報のARN |
| `STEP_FUNCTION_ARN` | Step Functions ARN |
| `ORDER_QUEUE_URL` | SQS注文キューURL |
| `SLACK_WEBHOOK_URL` | Slack通知用Webhook |
| `TRADING_PAIRS_CONFIG` | 通貨ペア設定JSON |
| `MODEL_BUCKET` | ONNXモデル格納S3バケット |
| `MODEL_PREFIX` | ONNXモデルのS3プレフィックス |
| `CRYPTOPANIC_API_KEY` | CryptoPanic APIキー |
| `MARKET_CONTEXT_TABLE` | マーケットコンテキストテーブル名 |
| `VOLATILITY_THRESHOLD` | 変動閾値（%） |
| `MAX_POSITION_JPY` | 最大ポジション額（円） |

### 通貨ペア設定 (TRADING_PAIRS_CONFIG)

```json
{
  "eth_usdt": {
    "binance": "ETHUSDT",
    "coincheck": "eth_jpy",
    "news": "ETH",
    "name": "Ethereum"
  },
  "btc_usdt": {
    "binance": "BTCUSDT",
    "coincheck": "btc_jpy",
    "news": "BTC",
    "name": "Bitcoin"
  }
}
```

- `binance`: Binance APIのシンボル名（価格取得用）
- `coincheck`: Coincheck APIのペア名（取引執行用）
- `news`: CryptoPanic APIの通貨コード（ニュース取得用）
- `name`: Slack通知等に使う表示名

---

## price-collector

5分間隔で全通貨の価格を Binance から収集し、変動を検知して分析をトリガー。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (5分間隔) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | prices (W), analysis_state (R/W) |

### 処理フロー

1. `TRADING_PAIRS_CONFIG` から全通貨ペアを取得
2. 各通貨について Binance API から5分足終値を取得
3. DynamoDB `prices` テーブルに保存
4. 1時間前の価格と比較して変動率を計算
5. いずれかの通貨が変動閾値(0.3%)超え、または1時間経過 → Step Functions 起動
6. **全通貨のペアリスト** を Step Functions に渡す（個別ではなく一括分析）

### 出力

```json
{
  "statusCode": 200,
  "body": {
    "pairs_collected": 6,
    "triggered": 2,
    "analysis_started": true
  }
}
```

### Step Functions への入力

```json
{
  "pairs": ["eth_usdt", "btc_usdt", "xrp_usdt", "sol_usdt", "doge_usdt", "avax_usdt"],
  "timestamp": 1770523800,
  "trigger_reasons": ["volatility", "periodic"]
}
```

---

## technical

DynamoDB から価格履歴を読み取り、テクニカル指標を計算してスコアを返す。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Map > Parallel) |
| メモリ | 512MB |
| タイムアウト | 60秒 |
| DynamoDB | prices (R) |

### 入力

```json
{
  "pair": "eth_usdt",
  "timestamp": 1770523800
}
```

### 出力

```json
{
  "pair": "eth_usdt",
  "technical_score": 0.432,
  "indicators": {
    "rsi": 45.23,
    "macd": 0.0012,
    "macd_signal": 0.0008,
    "sma_20": 2345.67,
    "sma_200": 2300.00,
    "bb_upper": 2400.00,
    "bb_lower": 2290.00,
    "golden_cross": true
  },
  "current_price": 2350.50
}
```

### 分析指標

| 指標 | パラメータ | スコアへの影響 |
|---|---|---|
| RSI | 14期間 | <30: 買い、>70: 売り |
| MACD | (12,26,9) | シグナルクロスで判定 |
| MACD histogram slope | 直近3本 | 正→縮小(slope<-0.3)で減速検知 |
| SMA | 20, 200 | ゴールデン/デッドクロス |
| Bollinger | (20,2) | バンド位置で判定 |

---

## chronos-caller

SageMaker Serverless Endpoint 上の Amazon Chronos-2 (120M params) を呼び出し、AI 価格予測を行う。分位数予測 (q10/q50/q90) + 確信度 (confidence) 付きのスコアを返す。推論失敗時はモメンタムベースの代替スコアにフォールバック。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Map > Parallel) |
| メモリ | 256MB |
| タイムアウト | 180秒 |
| DynamoDB | prices (R) |
| SageMaker | `eth-trading-chronos-base` (Serverless Endpoint) |

### 動作モード

| モード | 条件 | 動作 |
|---|---|---|
| SageMaker推論 | エンドポイント正常 | Chronos-2 で12ステップ先のAI価格予測 + 分位数予測 + 確信度算出 |
| フォールバック | SageMaker障害時 | モメンタムベーススコア（短期60% + 中期40%）、confidence=0.1 |

### SageMaker エンドポイント構成

| 項目 | 値 |
|---|---|
| エンドポイント名 | `eth-trading-chronos-base` |
| タイプ | Serverless (6144MB, MaxConcurrency=8) |
| アカウントクォータ | 全Serverlessエンドポイント合計のMaxConcurrency上限=10 |
| DLC Image | `huggingface-pytorch-inference:2.6.0-transformers4.49.0-cpu-py312-ubuntu22.04` |
| モデル格納 | `s3://eth-trading-sagemaker-models-652679684315/chronos-2/model.tar.gz` |
| 依存 | `chronos-forecasting>=2.2.0` (torch 2.6.0 は DLC にプリインストール済) |
| IAMロール | `eth-trading-sagemaker-execution-role` |
| Endpoint Config | `eth-trading-chronos-2-config` |
| Model Name | `eth-trading-chronos-2` |
| デプロイスクリプト | `scripts/deploy_sagemaker_chronos.py` |

#### 同時実行数の関係

```
AWSクォータ (10) ≥ エンドポイント MaxConcurrency (8) ≥ Step Functions MaxConcurrency (6)
```

- **AWSクォータ**: Service Quotas で申請・承認が必要なアカウントレベルの上限
- **エンドポイント MaxConcurrency**: エンドポイントが受け付ける同時リクエスト数 (超過時 ThrottlingException)
- **Step Functions MaxConcurrency**: Map State での同時実行ペア数

⚠️ クォータが承認されてもエンドポイント自体の MaxConcurrency を別途更新しないと反映されない。

### 予測パラメータ

| パラメータ | 値 | 環境変数 |
|---|---|---|
| 入力長 | 336本 (28h = 日次サイクル1周+α) | `INPUT_LENGTH` |
| 予測ステップ | 12 (= 1h先) | `PREDICTION_LENGTH` |
| スコアスケール | ±3% = ±1.0 | `SCORE_SCALE_PERCENT` |

### 予測 → スコア変換

予測系列の加重平均（遠い将来ほど重みが大きい）と現在価格の変化率から算出:
- `change_percent = (weighted_avg - current_price) / current_price × 100`
- ±3% の変動予測で ±1.0 にスケール
- 外れ値カット: ±20%超の予測を現在価格で置換
- トレンド加速ボーナス: 後半予測 > 前半予測で最大±0.15加算
- std減衰: CV > 5%でスコアを50%まで減衰

### リトライ設定

| パラメータ | 値 | 説明 |
|---|---|---|
| MAX_RETRIES | 5 | 最大リトライ回数 |
| BASE_DELAY | 3.0秒 | 基本待機時間 (SageMaker Serverless冷起動考慮) |
| MAX_DELAY | 45.0秒 | 最大待機時間 |
| アルゴリズム | 指数バックオフ + jitter | `delay = min(BASE_DELAY * 2^attempt, MAX_DELAY) + random(0.1-0.5)*delay` |

- ThrottlingException は `[INFO]` ログとして出力（想定内動作、エラーアラートをトリガーしない）
- 全リトライ失敗時はモメンタムフォールバックに自動切替

### 確信度 (confidence)

SageMaker側で分位数の広がりから算出 (0.0-1.0):
- std を q90-q10 の正規分布近似で推定: `std = (q90 - q10) / 2.56`
- 各ステップの `cv = std / |median|` → `confidence_step = 1 / (1 + cv * 10)`
- 全ステップの平均が `confidence`
- Aggregatorの動的ウェイトに使用 (#31)

### 出力

```json
{
  "pair": "eth_usdt",
  "chronos_score": 0.312,
  "confidence": 0.965,
  "prediction": [2355.2, 2361.8, 2358.5, ...],
  "prediction_std": [12.5, 15.3, 18.1, ...],
  "prediction_q10": [2342.7, 2346.5, 2340.4, ...],
  "prediction_q90": [2367.7, 2377.1, 2376.6, ...],
  "current_price": 2350.50,
  "data_points": 336,
  "model": "chronos-2"
}
```

---

## sentiment-getter

DynamoDB から最新のセンチメントスコアを取得して返す。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Map > Parallel) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | sentiment (R) |

### 出力

```json
{
  "pair": "eth_usdt",
  "sentiment_score": 0.65,
  "last_updated": 1770522000,
  "source": "cryptopanic"
}
```

---

## news-collector

CryptoPanic API から全通貨のニュースを取得し、通貨別にセンチメント分析してDynamoDBに保存。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (30分間隔) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | sentiment (W) |
| 外部API | CryptoPanic (2 calls/実行) |

### API最適化

```
1回目: ?currencies=ETH,BTC,XRP,SOL,DOGE,AVAX  → 全通貨ニュース一括
2回目: (通貨指定なし)                          → 全体市場ニュース
合計: 2 API calls × 48回/日 × 30日 = 2,880/月 (Growth Plan 3,000内)
```

### 通貨マッチング

CryptoPanic API v2 (Growth Plan) では、記事の通貨情報が `instruments` フィールドに格納される（v1の `currencies` もフォールバック対応）。

```json
"instruments": [{"code": "ETH", "title": "Ethereum", ...}]
```

### 通貨別センチメント計算

| ニュース種別 | 重み | 適用 |
|---|---|---|
| 直接関連 (例: ETHニュースをETHに) | ×1.0 | 対象通貨のみ |
| BTC相関 (BTCニュースを他通貨に) | ×0.5 | BTC以外の通貨 |
| 全体市場ニュース | ×0.3 | 全通貨 |

### センチメントスコアの決定

| 優先度 | 条件 | スコア決定方法 |
|---|---|---|
| 1 | 投票数 ≥ 5 | 賛否比率 × 信頼度係数 |
| 2 | 投票数 < 5 | タイトルキーワード分析（強気/弱気語検出） |
| 補助 | `panic_score` 存在時 | ±0.10 の微調整（0=ネガ, 2=中立, 4=ポジ） |

**タイトルキーワード分析**: surge, rally, breakout 等の強気語と crash, plunge, hack 等の弱気語を検出し、キーワード1つにつき±0.1（最大±0.3）をスコアに加算。

---

## aggregator

全通貨の分析結果を統合、マーケットコンテキストを加味してスコアランキングを作成し、最適な売買判定を行う。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Map完了後) |
| メモリ | 512MB |
| タイムアウト | 120秒 |
| DynamoDB | signals (W), positions (R), market-context (R) |

### 入力 (Step Functions Map の出力)

```json
{
  "analysis_results": [
    {
      "pair": "eth_usdt",
      "technical": {"technical_score": 0.65, ...},
      "chronos": {"chronos_score": 0.45, ...},
      "sentiment": {"sentiment_score": 0.72, ...}
    },
    ...
  ]
}
```

### 処理フロー

0. DynamoDB `market-context` テーブルから最新のマーケットコンテキストを取得
   - 2時間以上古い場合は中立 (0.0) として扱う
1. 各通貨の4成分加重平均スコアを計算 (ベース: Tech×0.45 + Chronos×0.25 + Sent×0.15 + MktCtx×0.15)
   - **確信度ベース動的ウェイト**: Chronos confidence に応じてウェイトを動的調整
     - `weight_shift = (confidence - 0.5) × 0.30` → クランプ [-0.15, +0.10]
     - 高確信度(1.0): Chronos 0.35, Tech 0.35 / 低確信度(0.0): Chronos 0.10, Tech 0.60
   - アルトコインはBTC Dominance補正: >60%で-0.05、<40%で+0.05
2. BB幅（ボリンジャーバンド幅）からボラティリティ適応型閾値を計算
   - `vol_ratio = avg_bb_width / baseline(0.03)` → クランプ 0.67〜2.0
   - `buy_threshold = 0.28 × vol_ratio`, `sell_threshold = -0.15 × vol_ratio`
   - F&G連動補正: F&G≤20 → `buy_threshold × 1.35`, F&G≥80 → `buy_threshold × 1.20` (SELL不変)
3. モメンタム減速チェック: 保有中通貨のMACDヒストグラムが正→縮小中(slope<-0.3)なら、SELL閾値を50%緩和して早期利確
3. 全通貨のシグナルを DynamoDB に保存（動的閾値・BB幅・market_context_scoreも記録）
4. 全通貨をスコア降順でランキング
5. 全通貨のアクティブポジションを取得（複数保有対応）
6. 売買判定（SELL優先、動的閾値で判定）:
   - SELL判定: 保有中の全ポジションについて、スコア <= sell_threshold → SELL
   - BUY判定: 未保有通貨の中で最高スコア >= buy_threshold → BUY
   - それ以外 → HOLD
7. シグナルがあれば SQS に注文メッセージ送信
8. Slack にランキング + 動的閾値 + 市場環境付き分析結果を通知

### 出力

```json
{
  "signal": "BUY",
  "target_pair": "eth_jpy",
  "target_score": 0.5234,
  "has_signal": true,
  "ranking": [
    {"pair": "eth_usdt", "name": "Ethereum", "score": 0.5234},
    {"pair": "btc_usdt", "name": "Bitcoin", "score": 0.3521},
    ...
  ],
  "active_positions": ["eth_jpy", "btc_jpy"],
  "buy_threshold": 0.2800,
  "sell_threshold": -0.1500,
  "market_context": {
    "market_score": 0.1468,
    "components": {
      "fear_greed": {"value": 14, "score": 0.397},
      "funding_rate": {"avg_rate": -0.0066, "score": 0.133},
      "btc_dominance": {"value": 56.86, "score": -0.457}
    }
  }
}
```

---

## order-executor

SQSから注文メッセージを受信し、Coincheck APIで成行注文を実行。

| 項目 | 値 |
|---|---|
| トリガー | SQS (order-queue, batch=1) |
| メモリ | 256MB |
| タイムアウト | 30秒 |
| DynamoDB | positions (R/W), trades (W) |
| 外部API | Coincheck |

### 同一通貨重複防止

BUY注文時、対象通貨のアクティブポジションが既に存在するかチェック。例:

```
ETH保有中にETHのBUYシグナル → 既にETH保有中のためスキップ（Slack通知）
ETH保有中にBTCのBUYシグナル → 異なる通貨なのでBTC購入を実行
```

### Coincheck API 呼び出し

```
買い: POST /api/exchange/orders
      { pair: "eth_jpy", order_type: "market_buy", market_buy_amount: "5000" }

売り: POST /api/exchange/orders
      { pair: "eth_jpy", order_type: "market_sell", amount: "0.01" }
```

認証: HMAC-SHA256 署名（Secrets Manager からキー取得）

### 通貨別注文ルール

Coincheck 取引所の通貨別最小注文数量・小数点以下桁数に基づき、売り注文時にバリデーションを実施。

| 通貨 | 最小注文数量 | 小数点桁数 |
|---|---|---|
| BTC | 0.001 | 8桁 |
| ETH | 0.001 | 8桁 |
| XRP | 1.0 | 6桁 |
| SOL | 0.01 | 8桁 |
| DOGE | 1.0 | 2桁 |
| AVAX | 0.01 | 8桁 |

参考: [取引注文ルール](https://faq.coincheck.com/s/article/40218?language=ja) / [取引所手数料](https://coincheck.com/ja/exchange/fee) / [取引所 API](https://coincheck.com/ja/documents/exchange/api)

---

## position-monitor

5分間隔で全通貨のアクティブポジションを監視し、SL/TP判定を実行。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (5分間隔) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | positions (R/W) |
| 外部API | Coincheck (価格取得) |

### 処理フロー

1. `TRADING_PAIRS_CONFIG` の全通貨についてアクティブポジションを検索
2. ポジションがあれば Coincheck API で現在価格を取得
3. **ピーク価格追跡**: `highest_price` を更新し DynamoDB に永続化
4. **連続トレーリングストップ**:
   - ピーク利益 3-5% → ピークから 2.0% 下でSL
   - ピーク利益 5-8% → ピークから 1.5% 下でSL
   - ピーク利益 8-12% → ピークから 1.2% 下でSL
   - ピーク利益 12%+ → ピークから 1.0% 下でSL
   - 3%以上到達後は必ず建値以上を保証
5. SL/TP 判定:
   - 現在価格 <= ストップロス(参入-5%、またはトレーリングSL) → 売り指示
   - 現在価格 >= テイクプロフィット(参入+10%) → 売り指示
6. 売り指示は SQS 経由で order-executor に送信

---

## warm-up

初回デプロイ時に Binance から全通貨の過去データを取得して DynamoDB に投入。手動で1回実行する。

| 項目 | 値 |
|---|---|
| トリガー | 手動実行 |
| メモリ | 512MB |
| タイムアウト | 300秒 |
| DynamoDB | prices (W) |

### 使い方

```bash
# 全通貨の過去データ投入
aws lambda invoke --function-name eth-trading-warm-up \
  --payload '{}' --cli-binary-format raw-in-base64-out output.json

# 特定通貨のみ
aws lambda invoke --function-name eth-trading-warm-up \
  --payload '{"pair": "btc_usdt"}' --cli-binary-format raw-in-base64-out output.json
```

各通貨1000本（約3.5日分）の5分足データを投入。テクニカル分析（SMA200等）に必要な初期データ。

---

## error-remediator

CloudWatch Logs のエラーパターンを検知し、Slack通知 + GitHub Actions 自動修復パイプラインをトリガー。

| 項目 | 値 |
|---|---|
| トリガー | CloudWatch Subscription Filter (8 Lambda) |
| メモリ | 256MB |
| タイムアウト | 30秒 |
| DynamoDB | error-remediator-cooldown (R/W) |
| 外部API | Slack Webhook, GitHub API |

### 処理フロー

1. CloudWatch Subscription Filter からエラーログイベントを受信
2. Base64 + gzip デコードしてエラーメッセージを抽出
3. DynamoDB でクールダウン確認（同一関数は30分間隔）
4. Slack にエラー内容を即座に通知
5. GitHub API で `repository_dispatch` イベントを送信
6. GitHub Actions が Claude AI でエラー分析 → コード修正 → デプロイ → 検証

### クールダウン

| 項目 | 値 |
|---|---|
| クールダウン時間 | 30分 |
| スコープ | Lambda関数ごと |
| 保存先 | DynamoDB (TTL: 24時間) |

同一関数のエラーが30分以内に再発した場合は、重複トリガーを防止してスキップ。

### GitHub Actions 連携

```
error-remediator → GitHub API (repository_dispatch)
                      → auto-fix-errors.yml ワークフロー
                          → Claude Sonnet でエラー分析
                          → コード修正 → デプロイ → 検証
                          → 成功時: git push + Slack通知
                          → 失敗時: Slack通知のみ
```

---

## slack-notifier (内部Lambda)

Terraform で自動生成されるインライン Lambda。SNS メッセージを Slack に転送する。

| 項目 | 値 |
|---|---|
| トリガー | SNS (alerts) |
| メモリ | 128MB |
| コード | Terraform内に定義（インライン） |

DLQ滞留等のシステムアラートを Slack Webhook に転送。取引通知は order-executor / position-monitor が直接 Slack Webhook に送信。

---

## daily-reporter (Phase 4 新設)

毎日 23:00 JST に実行。1日の取引・シグナル・市場データを集計し、S3保存 + Slack通知 + GitHub Actions 自動改善トリガー。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (毎日 14:00 UTC = 23:00 JST) |
| メモリ | 512MB |
| タイムアウト | 120秒 |
| DynamoDB | trades (R), signals (R), positions (R), market-context (R), improvements (R/W) |
| S3 | daily-reports (W) |
| 外部連携 | Slack Webhook, GitHub API (repository_dispatch) |

### 処理フロー

1. 全通貨ペアの直近24h/7d/30dの取引履歴を集計
2. 直近24hのシグナル統計を算出（コンポーネント別near_zero率含む）
3. アクティブポジション・市場コンテキスト・改善履歴を取得
4. 構造化レポートを生成 → S3に保存 (90日ライフサイクル)
5. Slackに日次サマリーを投稿
6. `repository_dispatch` で `daily-improvement` イベントをトリガー
7. GitHub Actions (auto-improve.yml) がClaude AIで分析 → 自動改善

### GitHub Actions 連携

```
daily-reporter → GitHub API (repository_dispatch)
                    → auto-improve.yml ワークフロー
                        → Claude Sonnet で日次データ分析
                        → NO_ACTION / PARAM_TUNE / CODE_CHANGE 判定
                        → コード変更 → デプロイ → docs更新 → git push
                        → DynamoDB improvements テーブルに記録
                        → Slack通知
```

### 安全ルール

| ルール | 値 |
|---|---|
| ウェイト変更幅 | 1回 ±0.05 以内、合計1.0維持 |
| 閾値変更幅 | 1回 ±0.03 以内 |
| 変更頻度 | 直近2週間以内は効果測定のため変更控え |
| 最低データ量 | 日次3件以上のトレード |
| ロールバック | improvements テーブルにbefore/after記録 |

---

## market-context (Phase 3 新設)

30分間隔でマクロ市場環境指標を収集し、DynamoDB に保存。Aggregator が直接読み取って4番目の分析成分として使用。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (30分間隔) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | market-context (W) |
| 外部API | Alternative.me, Binance Futures, CoinGecko |

### 処理フロー

1. Alternative.me API から Fear & Greed Index を取得
2. Binance Futures API から主要通貨のファンディングレートを取得
3. CoinGecko Global API から BTC Dominance を取得
4. 3つのサブスコアを加重平均してマーケットスコアを算出
5. DynamoDB `market-context` テーブルに保存 (TTL: 14日)

### 外部API

| API | エンドポイント | 取得データ | コスト |
|---|---|---|---|
| Alternative.me | `api.alternative.me/fng/` | Fear & Greed Index (0-100) | 無料 |
| Binance Futures | `fapi.binance.com/fapi/v1/premiumIndex` | ファンディングレート | 無料 |
| CoinGecko | `api.coingecko.com/api/v3/global` | BTC Dominance (%) | 無料 |

### スコア計算

```
market_score = fng_score × 0.50 + funding_score × 0.30 + dominance_score × 0.20
```

| サブスコア | 重み | ロジック |
|---|---|---|
| F&G Score | 50% | 逆張り: Extreme Fear(0-10)→+1.0, Extreme Greed(90-100)→-1.0 |
| Funding Score | 30% | 逆符号: 正のfunding(ロング過多)→売り圧力、負→買いチャンス |
| Dominance Score | 20% | 50%を中立として ±15%で±1.0にスケール |

### 出力 (DynamoDB)

```json
{
  "context_type": "market_context",
  "timestamp": 1770523800,
  "market_score": 0.1468,
  "components": {
    "fear_greed": {"value": 14, "classification": "Extreme Fear", "score": 0.397},
    "funding_rate": {"symbols": ["BTCUSDT", "ETHUSDT", ...], "avg_rate": -0.000066, "score": 0.133},
    "btc_dominance": {"value": 56.86, "score": -0.457}
  },
  "ttl": 1771733400
}
```

### 設計上の注意点

- Step Functions パイプラインには**含まれない**（独立した EventBridge スケジュール）
- Aggregator が DynamoDB から直接読み取る（2時間以上古い場合は中立扱い）
- 全通貨で同一スコアを適用（マクロ環境は通貨間で共通）
- BTC Dominance によるアルトコイン補正は Aggregator 側で実施

---

## データフロー図

```mermaid
flowchart TD
    subgraph 定期実行
        E1["5分毎"] --> PC["price-collector"]
        E2["5分毎"] --> PM["position-monitor"]
        E3["30分毎"] --> NC["news-collector"]
        E4["30分毎"] --> MC["market-context"]
    end

    subgraph Step Functions
        PC -->|"全通貨リスト"| MAP["Map State"]
        MAP --> TECH["technical ×6"]
        MAP --> CHRON["chronos-caller ×6"]
        MAP --> SENT["sentiment-getter ×6"]
        TECH & CHRON & SENT --> AGG["aggregator"]
    end

    subgraph 外部API
        MC -->|"Fear & Greed"| ALT["Alternative.me"]
        MC -->|"Funding Rate"| BIN["Binance Futures"]
        MC -->|"BTC Dominance"| CG["CoinGecko"]
    end

    subgraph DynamoDB
        PC -->|"W"| DB_P["prices"]
        PC -->|"R/W"| DB_ST["analysis_state"]
        NC -->|"W"| DB_S["sentiment"]
        MC -->|"W"| DB_MC["market-context"]
        TECH -->|"R"| DB_P
        CHRON -->|"R"| DB_P
        SENT -->|"R"| DB_S
        AGG -->|"R"| DB_MC
        AGG -->|"W"| DB_SIG["signals"]
        AGG -->|"R"| DB_POS["positions"]
        OE -->|"R/W"| DB_POS
        OE -->|"W"| DB_T["trades"]
        PM -->|"R"| DB_POS
    end

    subgraph 注文実行
        AGG -->|"SQS"| OE["order-executor"]
        PM -->|"SQS"| OE
        OE -->|"Coincheck API"| TRADE["取引"]
    end

    subgraph 監視・自動修復
        CW["CloudWatch Logs"] -->|"Subscription Filter"| ER["error-remediator"]
        ER -->|"Slack通知"| SLACK["Slack"]
        ER -->|"repository_dispatch"| GH["GitHub Actions<br/>Claude自動修復"]
    end

    subgraph 自動改善パイプライン
        E5["毎日23:00 JST"] --> DR["daily-reporter"]
        DR -->|"R"| DB_T
        DR -->|"R"| DB_SIG
        DR -->|"R"| DB_POS
        DR -->|"R"| DB_MC
        DR -->|"W"| S3_RPT["S3 daily-reports"]
        DR -->|"Slack"| SLACK
        DR -->|"repository_dispatch"| AI["GitHub Actions<br/>Claude自動改善"]
        AI -->|"W"| DB_IMP["improvements"]
    end
```
