# Lambda関数リファレンス

全11個の Lambda 関数の仕様、入出力、設定の詳細。

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
| `SLACK_WEBHOOK_URL` | Slack通知用Webhook |
| `TRADING_PAIRS_CONFIG` | 通貨ペア設定JSON |
| `SAGEMAKER_ENDPOINT` | SageMaker Serverless エンドポイント名 |
| `MODEL_BUCKET` | S3モデル格納バケット（レガシー） |
| `MODEL_PREFIX` | S3モデルのプレフィックス（レガシー） |
| `CRYPTOPANIC_API_KEY` | CryptoPanic APIキー |
| `MARKET_CONTEXT_TABLE` | マーケットコンテキストテーブル名 |
| `TF_SCORES_TABLE` | TF別スコアテーブル名 |
| `BEDROCK_MODEL_ID` | Bedrock LLMモデルID (センチメント分析) |
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

Step Functions Phase 1 でTF別に全通貨の価格を Binance から収集し、DynamoDB に保存。分析トリガーは行わず、純粋な価格収集のみ。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Phase 1: CollectPrices) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | prices (W) |

### 処理フロー

1. `TRADING_PAIRS_CONFIG` から全通貨ペアを取得
2. 各通貨について Binance API から5分足OHLCVを取得
3. DynamoDB `prices` テーブルに保存

### 出力

```json
{
  "statusCode": 200,
  "body": {
    "pairs_collected": 3,
    "errors": 0
  }
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
| DynamoDB | prices (R), analysis_state (W) |

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
| DynamoDB | prices (R), analysis_state (W) |
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

### Chronos-2 推論パラメータ詳細

#### Lambda側 → SageMaker (リクエスト)

Lambda (`chronos-caller`) から SageMaker エンドポイントに送信される JSON ペイロード:

```json
{
  "context": [2350.1, 2352.3, 2348.7, ...],
  "prediction_length": 12
}
```

| パラメータ | 型 | 値 | 説明 |
|---|---|---|---|
| `context` | `float[]` | 336要素 | 価格履歴 (Typical Price = (High+Low+Close)/3)。OHLCがない古いレコードはCloseにフォールバック |
| `prediction_length` | `int` | 12 | 5分足×12ステップ = 1時間先を予測 |

#### SageMaker推論コード (`inference.py`) の内部パラメータ

`scripts/deploy_sagemaker_chronos.py` 内で生成される `inference.py` が使用するパラメータ:

| パラメータ | 値 | 説明 |
|---|---|---|
| モデル | `amazon/chronos-2` | HuggingFace Hub から自動ダウンロード (初回起動時) |
| Pipeline | `BaseChronosPipeline` | Chronos-2 の分位数直接出力パイプライン (サンプリング不要) |
| `device_map` | `"cpu"` | CPU推論 (Serverless は GPU 非対応) |
| `torch_dtype` | `torch.float32` | 精度 (CPU なので float32) |
| `quantile_levels` | `[0.1, 0.5, 0.9]` | 出力する分位数レベル (10th, 50th, 90th パーセンタイル) |
| `limit_prediction_length` | `False` | 予測長の制限を無効化 |
| 入力テンソル形状 | `(1, 1, T)` | (batch=1, variates=1, time=336) — univariate 時系列 |

#### SageMaker → Lambda (レスポンス)

`predict_quantiles()` の出力を変換して返却:

| フィールド | 型 | 算出方法 |
|---|---|---|
| `median` | `float[]` | 50th パーセンタイル (`q[:, 1]`) |
| `mean` | `float[]` | `mean_list` から取得 |
| `std` | `float[]` | `abs(q90 - q10) / 2.56` (正規分布近似: q90-q10 ≈ 2.56σ) |
| `q10` | `float[]` | 10th パーセンタイル (`q[:, 0]`) — 悲観シナリオ |
| `q90` | `float[]` | 90th パーセンタイル (`q[:, 2]`) — 楽観シナリオ |
| `confidence` | `float` | 各ステップの `1/(1 + cv*10)` の平均 (`cv = std/|median|`) |
| `confidence_per_step` | `float[]` | 各予測ステップごとの確信度 |
| `model` | `str` | `"chronos-2"` |

#### Lambda側スコアリング (`predictions_to_score`)

SageMaker レスポンスの `median` をトレーディングスコア (-1.0 〜 +1.0) に変換:

| 処理 | 詳細 |
|---|---|
| 外れ値カット | ±20%超の予測値を現在価格に置換 |
| 加重平均 | 重み `w[i] = (i+1)/n` — 遠い将来ほど重い |
| スケーリング | `score = change_percent / 3.0` (±3%変動 = ±1.0) |
| トレンド加速ボーナス | 後半予測 > 前半予測で最大±0.15加算 (予測点6以上の場合) |
| std 減衰 | CV>5%でスコアを50%まで減衰、CV<1%は100%維持 |
| クリッピング | 最終スコアを `[-1.0, +1.0]` に制限 |

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
1回目: ?currencies=BTC,ETH,XRP               → 全通貨ニュース一括
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
| 2 | 投票数 < 5 | AWS Bedrock (Amazon Nova Micro) によるLLMセンチメント分析 |
| 3 | LLM失敗時 | ルールベースNLPフォールバック（キーワード分析） |
| 補助 | `panic_score` 存在時 | ±0.10 の微調整（0=ネガ, 2=中立, 4=ポジ） |

**LLMセンチメント分析**: 投票不足の全記事タイトルをバッチで1回のAPI呼び出しで分析。暠定語や文脈を考慮した高精度なスコアを返す。コスト: ~$2/月。

---

## aggregator

全通貨の分析結果を統合、マーケットコンテキストを加味してスコアランキングを作成し、通貨毎にBUY/SELL/HOLDを判定。

| 項目 | 値 |
|---|---|
| トリガー | Step Functions (Map完了後) / EventBridge (15分間隔, meta_aggregateモード) |
| メモリ | 512MB |
| タイムアウト | 120秒 |
| DynamoDB | signals (W), tf-scores (R/W), market-context (R) |

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
1. 各通貨の4成分加重平均スコアを計算 (ベース: Tech×0.35 + Chronos×0.35 + Sent×0.15 + MktCtx×0.15)
   - **確信度ベース動的ウェイト**: Chronos confidence に応じてウェイトを動的調整
     - `weight_shift = (confidence - 0.5) × 0.16` → クランプ [-0.08, +0.08]
     - 高確信度(1.0): Chronos 0.43, Tech 0.27 / 低確信度(0.0): Chronos 0.27, Tech 0.43
   - アルトコインはBTC Dominance補正: >60%で-0.05、<40%で+0.05
2. BB幅（ボリンジャーバンド幅）からボラティリティ適応型閾値を計算
   - `vol_ratio = avg_bb_width / baseline(0.03)` → クランプ 0.67〜2.0
   - `buy_threshold = 0.25 × vol_ratio`, `sell_threshold = -0.13 × vol_ratio`
   - F&G連動補正: F&G≤20 → `buy_threshold × 1.35`, F&G≥80 → `buy_threshold × 1.20` (SELL不変)
3. 全通貨のシグナルを DynamoDB に保存（動的閾値・BB幅・market_context_scoreも記録）
4. 全通貨をスコア降順でランキング
5. **通貨毎にBUY/SELL/HOLD判定（ポジション非依存）**:
   - スコア >= buy_threshold → BUY
   - スコア <= sell_threshold → SELL
   - それ以外 → HOLD
6. BUY/SELLがある場合、全判定を DynamoDB signals テーブルに保存
   - order-executor が EventBridge 15分毎に読み取って執行
   - 全てHOLDの場合もシグナルとして保存
7. Slack にランキング + 通貨別判定 + 市場環境付き分析結果を通知

### 出力

```json
{
  "decisions": [
    {"pair": "eth_usdt", "coincheck_pair": "eth_jpy", "signal": "BUY", "score": 0.5234},
    {"pair": "btc_usdt", "coincheck_pair": "btc_jpy", "signal": "HOLD", "score": 0.3521},
    {"pair": "xrp_usdt", "coincheck_pair": "xrp_jpy", "signal": "SELL", "score": -0.1800}
  ],
  "summary": {"buy": 1, "sell": 1, "hold": 1},
  "has_signal": true,
  "ranking": [
    {"pair": "eth_usdt", "name": "Ethereum", "score": 0.5234},
    {"pair": "btc_usdt", "name": "Bitcoin", "score": 0.3521},
    ...
  ],
  "active_positions": ["eth_jpy"],
  "buy_threshold": 0.2800,
  "sell_threshold": -0.1500,
  "timestamp": 1234567890
}
```

### DynamoDB シグナル保存形式

aggregator が DynamoDB signals テーブルに保存するシグナルデータ:

```json
{
  "pair": "eth_usdt",
  "timestamp": 1234567890,
  "signal": "BUY",
  "score": 0.5234,
  "analysis_context": {
    "components": {"technical": 0.812, "chronos": 0.654, "sentiment": 0.15, "market_context": 0.10},
    "buy_threshold": 0.2800,
    "sell_threshold": -0.1500,
    "weights": {"technical": 0.28, "chronos": 0.42, "sentiment": 0.15, "market_context": 0.15},
    "chronos_confidence": 0.85
  }
}
```

---

## order-executor

EventBridge 15分毎の定期起動で DynamoDB signals テーブルから最新シグナルを読み取り、Coincheck APIで成行注文を実行。

| 項目 | 値 |
|---|---|
| トリガー | EventBridge (15分間隔) |
| メモリ | 256MB |
| タイムアウト | 60秒 |
| DynamoDB | signals (R), positions (R/W), trades (W) |
| 外部API | Coincheck |

### バッチ注文処理

aggregatorが DynamoDB signals テーブルに全通貨のBUY/SELL/HOLD判定を保存。order-executorはポジション・残高を確認して実際の注文を実行する。

**処理順序**:
1. **SELL先**: ポジションがあれば売却、なければスキップ（資金確保）
2. **BUY**: スコア降順で処理、各通貨のポジション・残高を確認して購入

**BUYが複数ある場合**:
- スコア順（期待値の高い通貨が優先）
- 各BUYで残高確認 → Kelly/フォールバック比率で投資額算出
- 残高が減るため、優先度の低い通貨は自然と投資額が小さくなる
- MIN_ORDER_JPY(¥500)未満になると購入されない

**SELLが複数ある場合**:
- ポジションがあれば順に売却
- ポジションがない通貨はスキップ（無視）

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
   - 現在価格 >= テイクプロフィット(参入+30%) → 売り指示
6. 売り指示時は SQS 経由で order-executor に送信（ORDER_QUEUE_URL が設定されている場合）または Slack 通知のみ

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

CloudWatch Logs のエラーパターンを検知し、Slack通知を送信。

| 項目 | 値 |
|---|---|
| トリガー | CloudWatch Subscription Filter (8 Lambda) |
| メモリ | 256MB |
| タイムアウト | 30秒 |
| DynamoDB | analysis-state (R/W) |
| 外部API | Slack Webhook |

### 処理フロー

1. CloudWatch Subscription Filter からエラーログイベントを受信
2. Base64 + gzip デコードしてエラーメッセージを抽出
3. DynamoDB でクールダウン確認（同一関数は30分間隔）
4. Slack にエラー内容を即座に通知

### クールダウン

| 項目 | 値 |
|---|---|
| クールダウン時間 | 30分 |
| スコープ | Lambda関数ごと |
| 保存先 | DynamoDB (TTL: 24時間) |

同一関数のエラーが30分以内に再発した場合は、重複通知を防止してスキップ。

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
| Binance Futures | `fapi.binance.com/fapi/v1/fundingRate` | ファンディングレート | 無料 |
| CoinGecko | `api.coingecko.com/api/v3/global` | BTC Dominance (%) | 無料 |

### スコア計算

```
market_score = fng_score × 0.30 + funding_score × 0.35 + dominance_score × 0.35
```

| サブスコア | 重み | ロジック |
|---|---|---|
| F&G Score | 30% | 逆張り: Extreme Fear(0-10)→+0.30 (cap), Extreme Greed(90-100)→-0.30 (cap) + トレンド減衰 |
| Funding Score | 35% | 逆符号: 正のfunding(ロング過多)→売り圧力、負→買いチャンス |
| Dominance Score | 35% | 50%を中立として ±15%で±1.0にスケール |

### 出力 (DynamoDB)

```json
{
  "context_type": "global",
  "timestamp": 1770523800,
  "market_score": 0.1468,
  "fng_value": 14,
  "fng_classification": "Extreme Fear",
  "fng_score": 0.397,
  "funding_score": 0.133,
  "dominance_score": -0.457,
  "avg_funding_rate": -0.000066,
  "btc_dominance": 56.86,
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
        E2["5分毎"] --> PM["position-monitor"]
        E3["30分毎"] --> NC["news-collector"]
        E4["30分毎"] --> MC["market-context"]
        E5["TF別 15m/1h/4h/1d"] --> SF
        E6["15分毎"] --> META["aggregator(meta)"]
        E7["15分毎"] --> OE["order-executor"]
    end

    subgraph Step Functions
        SF["Map State ×3通貨"]
        SF --> PC["price-collector ×3"]
        SF --> TECH["technical ×3"]
        SF --> CHRON["chronos-caller ×3"]
        SF --> SENT["sentiment-getter ×3"]
        TECH & CHRON & SENT --> AGG["aggregator ×3"]
    end

    subgraph 外部API
        MC -->|"Fear & Greed"| ALT["Alternative.me"]
        MC -->|"Funding Rate"| BIN["Binance Futures"]
        MC -->|"BTC Dominance"| CG["CoinGecko"]
    end

    subgraph DynamoDB
        PC -->|"W"| DB_P["prices"]
        NC -->|"W"| DB_S["sentiment"]
        MC -->|"W"| DB_MC["market-context"]
        TECH -->|"R"| DB_P
        TECH -->|"W"| DB_TF["tf-scores"]
        CHRON -->|"R"| DB_P
        SENT -->|"R"| DB_S
        AGG -->|"R"| DB_MC
        AGG -->|"R"| DB_TF
        AGG -->|"W"| DB_SIG["signals"]
        AGG -->|"R"| DB_POS["positions"]
        META -->|"R"| DB_SIG
        META -->|"W"| DB_SIG
        OE -->|"R"| DB_SIG
        OE -->|"R/W"| DB_POS
        OE -->|"W"| DB_T["trades"]
        PM -->|"R"| DB_POS
    end

    subgraph 注文実行
        OE -->|"Coincheck API"| TRADE["取引"]
    end

    subgraph 監視・通知
        CW["CloudWatch Logs"] -->|"Subscription Filter"| ER["error-remediator"]
        ER -->|"通知"| SLACK["Slack"]
    end
```
