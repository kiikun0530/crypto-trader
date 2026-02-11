# バグ修正履歴・設計上の注意点

このドキュメントは、本番運用中に発生した重大バグとその修正内容をまとめたものです。  
将来のメンテナンス時に同種のバグを再発させないための知識ベースです。

---

## 目次

1. [通貨単位の二重性（USD / JPY）](#1-通貨単位の二重性usd--jpy)
2. [Coincheck 成行注文の仕様](#2-coincheck-成行注文の仕様)
3. [SQS バッチ処理と raise 禁止ルール](#3-sqs-バッチ処理と-raise-禁止ルール)
4. [BUY→即SELL 問題](#4-buy即sell-問題)
5. [インシデント全体タイムライン](#5-インシデント全体タイムライン)
6. [02/09 ゴミデータ事件（order_id未指定フォールバック）](#6-0209-ゴミデータ事件order_id未指定フォールバック)
7. [02/10 Extreme Fear損失とF&G BUY抑制](#7-0210-extreme-fear損失とfg-buy抑制)
8. [02/11 SageMaker Serverless ThrottlingException 頻発](#8-0211-sagemaker-serverless-throttlingexception-頻発)
9. [02/12 TP/トレーリングストップ矛盾（デッドコード）](#9-0212-tpトレーリングストップ矛盾デッドコード)
10. [02/12 DynamoDB Limit+FilterExpression アンチパターン](#10-0212-dynamodb-limitfilterexpression-アンチパターン)
11. [02/12 Bedrock nova-micro オンデマンド呼び出し廃止](#11-0212-bedrock-nova-micro-オンデマンド呼び出し廃止)

---

## 1. 通貨単位の二重性（USD / JPY）

### 概要

本システムは **2つの取引所** を使い、**異なる通貨単位** の価格データを扱う。

| データソース | API | 通貨単位 | 用途 |
|-------------|-----|---------|------|
| Binance | `api.binance.com/api/v3/klines` | **USDT（≒ USD）** | 価格分析（テクニカル、Chronos AI） |
| Coincheck | `coincheck.com/api/ticker` | **JPY** | 実売買、ポジション管理、P/L計算 |

### 価格の流れ

```
Binance (USD) → price-collector → DynamoDB prices表 (pair: eth_usdt, price: ~$2,100)
                                          ↓
                                   technical → score_pair() の current_price_usd: ~$2,100
                                   chronos-caller → 予測もUSD
                                          ↓
                              aggregator → スコア計算（USD世界で完結 → OK）

Coincheck (JPY) → order-executor → DynamoDB positions表 (pair: eth_jpy, entry_price: ~¥333,000)
                                          ↓
                              position-monitor → SL/TP判定（JPY同士 → OK）
                              aggregator → P/L表示（JPYで取得する必要あり → get_current_price()）
```

### ルール

- `score_pair()` の `current_price_usd` は **Binance USDT** → BB幅計算にのみ使用
- ポジションの `entry_price` は **Coincheck JPY** → P/L計算には JPY価格が必要
- P/L計算には必ず `get_current_price(pair)` で **Coincheck ticker API** から JPY取得
- `get_current_price()` は同じ関数名でも `price-collector` 版は **Binance USD** を返す

### 過去のバグ（2026-02-08）

`notify_slack()` で `scored_pairs[].current_price`（Binance USD: ETH ~$2,106）を  
ポジションの `entry_price`（Coincheck JPY: ETH ~¥333,057）と比較してP/L計算。  
→ 「含み損益: ¥-53,011 (-99.37%)」と表示された。  
**修正**: Coincheck ticker APIからJPY価格を取得するように変更。

---

## 2. Coincheck 成行注文の仕様

### 重要: レスポンスに約定情報が含まれない

Coincheck の `market_buy` / `market_sell` のレスポンスは:

```json
{
  "success": true,
  "id": 8645738021,
  "amount": null,    // ← 常にnull
  "rate": null,       // ← 常にnull
  "order_type": "market_buy",
  ...
}
```

**約定データは非同期** で確定し、別APIから取得する必要がある。

### 約定情報の取得方法

```
GET /api/exchange/orders/transactions?order_id={id}&limit=100
```

### 注意点（過去のバグ原因）

#### 問題1: `limit` パラメータ未指定（デフォルト少数）

約定は **複数トランザクションに分割** されることがある。  
`limit` 未指定だと一部しか返らず、**約定量が実際より少なく記録される**。

```python
# ❌ BAD: limitなし → 部分的なデータのみ
result = call_coincheck_api(f'/api/exchange/orders/transactions?order_id={order_id}', ...)

# ✅ GOOD: limit=100
result = call_coincheck_api(f'/api/exchange/orders/transactions?order_id={order_id}&limit=100', ...)
```

#### 問題2: `funds` の値に `abs()` が必要

トランザクションの `funds` フィールドは正負が混在する:

```json
{
  "funds": {
    "btc": "0.0051838",   // 買い手は正
    "jpy": "-58396.7"     // 買い手はJPYが負
  }
}
```

```python
# ❌ BAD: 符号を考慮せず合算
total_amount = sum(float(t['funds'][currency]) for t in transactions)

# ✅ GOOD: abs() で絶対値
total_amount = sum(abs(float(t['funds'][currency])) for t in transactions)
```

#### 問題3: 部分約定 → 誤った平均レート → 異常な entry_price

`limit` 未指定 + `abs()` なし → 取得量が実際より少ない  
→ `avg_rate = total_jpy / total_amount` が異常に高くなる  
→ `entry_price` が実際の8〜10倍で保存される  
→ `stop_loss = entry_price * 0.95` も異常に高い  
→ position-monitor が即座にSTOP_LOSS判定 → 即売り

**実際に発生した例**:

| 通貨 | 記録された entry_price | 実際の市場価格 | 倍率 |
|------|----------------------|--------------|------|
| BTC | ¥89,672,575 | ¥11,222,770 | 8x |
| ETH | ¥3,622,264 | ¥333,000 | 10x |

### 関連ソースコード

- `services/order-executor/handler.py`
  - `get_market_buy_fill()` — 買い約定取得
  - `get_market_sell_fill()` — 売り約定取得
  - `save_position()` — None/無効値のDecimalクラッシュ防止

---

## 3. SQS バッチ処理と raise 禁止ルール

### 背景

order-executor Lambda は SQS トリガーで起動し、**バッチ（複数メッセージ）** を一括処理する。

### 問題

```python
# ❌ DANGEROUS
def handler(event, context):
    for record in event['Records']:
        try:
            process_order(record)
        except Exception as e:
            raise  # ← これをやると全バッチが再配信される
```

SQS Lambda トリガーの仕様:
- `raise` → Lambda失敗 → **バッチ内の全メッセージ** が可視性タイムアウト後に再配信
- 既に成功した注文も再度 `process_order()` が実行される
- Coincheck API注文は冪等でないため、**二重注文** が発生

### 実際に発生した例

1. DOGE ¥100,000 の BUY 注文が SQS から配信
2. Coincheck API で成行買い成功
3. `save_position()` で `Decimal(str(None))` が発生し例外
4. `raise` で Lambda 失敗
5. SQS がメッセージを再配信
6. **同じ BUY 注文が再実行** → DOGE ¥32,000 追加購入（残高分）

### 修正ルール

```python
# ✅ SAFE: エラーはログ + Slack通知のみ、raiseしない
def handler(event, context):
    for record in event['Records']:
        try:
            process_order(record)
        except Exception as e:
            print(f"Error: {e}")
            send_notification('System', f'❌ 注文処理エラー\n{str(e)}')
            # raise しない → 残りのレコードは正常処理を継続
```

---

## 4. BUY→即SELL 問題

### 背景

以下の2つのソースからSELLメッセージが同一SQSキューに送信される:

1. **aggregator** — スコアがSELL閾値以下の場合
2. **position-monitor** — SL/TP に達した場合

### 問題パターン A: SQSバッチ内のBUY+SELL同居

```
SQSバッチ = [
  {pair: "btc_jpy", signal: "BUY", score: 0.35},     // aggregatorから
  {pair: "btc_jpy", signal: "SELL", score: -1.0}      // position-monitorから
]
```

executor が順番に処理:
1. BTC を Buy → ポジション保存  
2. **直後に** BTC を Sell → 即決済  

### 問題パターン B: 異常entry_price → 即STOP_LOSS

Bug #2 (fill取得バグ) で entry_price が8倍に → SL ¥85M設定  
→ position-monitor が次回起動時に BTC実価格(¥11M) < SL(¥85M) を検出  
→ SELL を SQS に送信 → executor が売却

### 修正

```python
_just_bought_pairs = set()  # グローバル変数

def execute_buy(pair, score):
    # ... 買い注文成功後 ...
    _just_bought_pairs.add(pair)  # 記録

def process_order(order):
    if signal == 'SELL':
        if pair in _just_bought_pairs:
            print(f"Skipping sell: just bought in this batch")
            return  # 同一バッチ内のSELLをブロック
```

---

## 5. インシデント全体タイムライン

2026-02-08 に発生した連鎖的バグの時系列:

```
14:13  DOGE ¥100K 成行買い
       → レスポンス: amount=None, rate=None
       → save_position() で Decimal(str(None)) クラッシュ
       → raise → SQSバッチ再配信
       → 同じBUY指示が再実行 → DOGE ¥32K 追加購入（二重注文）
       → ポジション未保存のまま

16:03  DOGE 8,504枚 成行売り
       → Coincheck売り成功
       → P/L計算で float(None) クラッシュ → raise
       → ただし close_position() は先に成功 → ポジション閉鎖済み
       → システムは「ポジションなし」と認識 → BUYループ開始

16:08  BTC ¥100K 成行買い
       → get_market_buy_fill() が部分約定データのみ取得（limit未指定）
       → 記録: amount=0.0051838 (実際は0.0089102)
       → 計算: avg_rate = ¥89,672,575（実際は¥11,222,770、8倍）
       → save_position(): entry_price=¥89.6M, stop_loss=¥85.2M

       position-monitor起動（EventBridge 5分間隔）
       → BTC現在価格¥11.2M < SL¥85.2M → STOP_LOSS判定
       → SELL btc_jpy を SQS に送信

16:08  BTC 0.0051838枚 成行売り（position-monitorのSTOP_LOSS）
       → 実際の保有量: 0.0089102
       → 売った量: 0.0051838（= fill取得で記録された量）
       → 残り: 0.0037264 BTC（孤児ポジション）

16:13  ETH ¥65.7K 成行買い → 同様のfillバグ → 即STOP_LOSS → 部分売り
       → 残り: 0.034587 ETH（孤児ポジション）
```

### 修正コミット

| コミット | 内容 |
|---------|------|
| `5ef65f9` | order-executor: handler raise除去, sell fill取得, batch即売り防止, fill計算修正 |
| `bfe42be` | aggregator: 複数ポジション表示 + 含み損益P/L対応 |
| `2e5cb9a` | aggregator: P/L表示でCoincheck JPY価格を使用（Binance USD混同修正） |

---

## 設計原則まとめ

| 原則 | 詳細 |
|------|------|
| **USD/JPY を混ぜない** | 分析はBinance USD、売買はCoincheck JPY。P/L計算は必ずJPY同士 |
| **Coincheck成行は必ずfill取得** | レスポンスのamount/rateは信用しない。transactions APIで取得 |
| **fill取得は limit=100 + abs()** | 複数トランザクション対応、符号混在対応 |
| **handler()でraiseしない** | エラーはログ+Slack通知。SQSバッチ再配信=二重注文のリスク |
| **同一バッチBUY→SELL防止** | `_just_bought_pairs` で同一Lambda実行内の即売りをブロック |
| **Decimalに渡す前にNoneチェック** | `float(x) if x else 0` + try/except で防御 |

---

## 6. 02/09 ゴミデータ事件（order_id未指定フォールバック）

### 概要

2026-02-09、`get_market_buy_fill()` が `order_id` なしで Coincheck `/api/exchange/orders/transactions` を呼び出すフォールバックパスが存在した。このパスでは直近の全トランザクション（他通貨含む）が混入し、約定レートが異常に膨張した。

### 影響

| テーブル | ゴミレコード数 | 特徴 |
|----------|----------------|------|
| eth-trading-trades | 101件 | score=0, threshold=0, rate=BTC 520M JPY (実勢14M) |
| eth-trading-positions | 39件 | 全closed, entry_price=BTC 520M / ETH 6.8M |

### 根本原因

```python
# ❌ BAD: order_idなしのフォールバック — 全通貨の直近トランザクションが返る
if not order_id:
    result = call_coincheck_api('/api/exchange/orders/transactions?limit=100', ...)

# ✅ GOOD: 修正済 — 必ずorder_idを指定
result = call_coincheck_api(f'/api/exchange/orders/transactions?order_id={order_id}&limit=100', ...)
```

加えて、50%乖離チェックが追加済み:
```python
# fill取得後のサニティチェック
if abs(avg_rate - ticker_rate) / ticker_rate > 0.50:
    raise ValueError(f"Fill rate {avg_rate} deviates >50% from ticker {ticker_rate}")
```

### クリーンアップ

手動スクリプトで異常レコードを削除:
- trades: 101件削除 → 12件の正常レコードが残存
- positions: 39件削除 → 19件残存 (1 open: sol_jpy)
- 判定基準: `MAX_SANE_PRICE` (btc=25M, eth=600K, xrp=500, sol=50K, doge=100, avax=10K) を超える `entry_price` / `rate`

### 再発防止

- `order_id` フィルタ必須 (修正済)
- 50%乖離チェック (修正済)
- `score=0 && threshold=0` のレコードは本来生成されないため、今後は不要

---

## 7. 02/10 Extreme Fear損失とF&G BUY抑制

### 概要

2026-02-10、Fear & Greed Index = 14 (Extreme Fear) の市場環境で、Chronos AIが異常に高いスコア (AI=+1.000) を出し、2件のトレードで合計 ¥3,162 の損失が発生。

### 損失詳細

| 通貨 | BUY Rate | SELL Rate | 損益 | 損失率 | AI Score |
|------|----------|-----------|------|--------|----------|
| ETH | ¥327,793 | ¥321,612 | ¥-1,661 | -1.89% | +1.000 |
| XRP | ¥225.4 | ¥220.2 | ¥-1,501 | -2.32% | 高スコア |

### 根本原因

- Chronos AI は過去の価格パターンのみで予測するため、市場全体のセンチメントを考慮しない
- Extreme Fear 市場では「反発」を予測するが、実際にはさらに下落するケースが多い
- 既存のBUY閾値はマクロ環境を考慮していなかった

### 修正: F&G連動BUY閾値抑制 (`bb5cfa2`)

`calculate_dynamic_thresholds()` に Market Context パラメータを追加:

```python
# F&G連動BUY閾値補正
FNG_FEAR_THRESHOLD = 20
FNG_GREED_THRESHOLD = 80
FNG_BUY_MULTIPLIER_FEAR = 1.35   # Extreme Fear: BUY閾値を35%引き上げ
FNG_BUY_MULTIPLIER_GREED = 1.20  # Extreme Greed: BUY閾値を20%引き上げ

fng_value = market_context.get('fear_greed', {}).get('value')
if fng_value is not None:
    if fng_value <= FNG_FEAR_THRESHOLD:
        buy_threshold *= FNG_BUY_MULTIPLIER_FEAR
    elif fng_value >= FNG_GREED_THRESHOLD:
        buy_threshold *= FNG_BUY_MULTIPLIER_GREED
```

- SELL閾値は変更なし (ストップロスは常に実行すべき)
- F&G=14の場合: `BUY_TH = 0.28 × vol_ratio × 1.35` → ¥-3,162の損失トレードをブロック
- Slack通知に `⚠️ F&G=14: BUY_TH ×1.35` 等の警告を追加

### 設計原則

| 原則 | 詳細 |
|------|------|
| **Extreme Fearは買い控え** | 市場恐怖時はAIスコアの信頼性が低下する。閾値引き上げで高確信度のみ通過 |
| **Extreme Greedは過熱警戒** | バブル時の天井掴みを防止。ただしFearほど厳しくない (×1.20 vs ×1.35) |
| **SELL閾値は不変** | 損切り・利確は市場環境に関係なく常に実行すべき |

---

## 8. 02/11 SageMaker Serverless ThrottlingException 頻発

### 概要

2026-02-11 01:18頃、`chronos-caller` Lambda から大量の ThrottlingException エラーが発生。6通貨ペアの並列分析で SageMaker Serverless Endpoint の同時実行上限を超過していた。CloudWatch Subscription Filter がリトライログもエラーとして検知し、error-remediator が連鎖的にトリガーされてエラーアラートが頻発した。

### エラー内容

```
ThrottlingException (attempt 1/5), waiting 2.4s...
ThrottlingException (attempt 2/5), waiting 4.7s...
Traceback (most recent call last): ...
```

### 根本原因（3層の問題）

| レイヤー | 問題 | 影響 |
|----------|------|------|
| SageMaker Endpoint | `MaxConcurrency = 2` のまま | 3つ目以降のリクエストが ThrottlingException |
| Step Functions | `MaxConcurrency` 未設定（無制限） | 6通貨ペアが全て同時に chronos-caller を呼び出し |
| Monitoring | `filter_pattern = "?ERROR ?Traceback ?Exception"` | 想定内のリトライログもエラーアラートをトリガー |

**重要な学び**: AWS Service Quotas の承認（アカウントレベルの上限=10）と、エンドポイント個別の `MaxConcurrency` 設定は**別物**。クォータが承認されても、エンドポイント自体の設定を変更しなければ反映されない。

```
AWSクォータ (10) ← アカウント全体の全Serverlessエンドポイントの MaxConcurrency 合計上限
    └→ エンドポイント MaxConcurrency (8) ← 1エンドポイントが受け付ける同時リクエスト数
         └→ Step Functions MaxConcurrency (6) ← Map State の同時実行ペア数
```

### 修正内容

#### 1. SageMaker Endpoint MaxConcurrency 引き上げ
```bash
# 新しい Endpoint Config を作成して適用
aws sagemaker create-endpoint-config \
  --endpoint-config-name eth-trading-chronos-base-config-v2 \
  --production-variants '[{"VariantName":"AllTraffic","ModelName":"eth-trading-chronos-base","InitialVariantWeight":1.0,"ServerlessConfig":{"MemorySizeInMB":6144,"MaxConcurrency":8}}]'

aws sagemaker update-endpoint \
  --endpoint-name eth-trading-chronos-base \
  --endpoint-config-name eth-trading-chronos-base-config-v2
```

#### 2. Step Functions MaxConcurrency 追加 (stepfunctions.tf)
```hcl
AnalyzeAllPairs = {
  Type           = "Map"
  MaxConcurrency = 6  # SageMaker MaxConcurrency=8 の範囲内
  ItemsPath      = "$.pairs"
  ...
}
```

#### 3. chronos-caller リトライ改善 (handler.py)
```python
# リトライ設定の調整
BASE_DELAY = 3.0   # 2.0 → 3.0s (SageMaker Serverless冷起動考慮)
MAX_DELAY = 45.0    # 30.0 → 45.0s

# ThrottlingException のログレベルを [INFO] に変更（エラーアラート回避）
print(f"[INFO] SageMaker throttled (attempt {attempt + 1}/{MAX_RETRIES}), "
      f"retrying in {total_delay:.1f}s - this is expected behavior")
```

#### 4. 監視フィルター改善 (monitoring.tf)
```hcl
filter_pattern = "?\"[ERROR]\" ?Traceback ?\"raise Exception\" -\"[INFO]\" -\"expected behavior\" -\"retrying in\""
```
- `[INFO]` を含むログを除外 → 想定内リトライがアラートをトリガーしない
- `[ERROR]` プレフィックス付きの真のエラーのみ検出

### 再発防止

| 対策 | 内容 |
|------|------|
| 同時実行数の階層設計 | クォータ(10) > Endpoint(8) > StepFunctions(6) でマージン確保 |
| リトライログの分類 | `[INFO]` / `[WARN]` / `[ERROR]` プレフィックスで監視フィルターと連携 |
| Endpoint Config のバージョン管理 | `config-v2` として新規作成 → エンドポイントに適用 |

---

## 9. 02/11 Chronos-T5-Base → Chronos-2 モデルアップグレード

### 概要

- **日時**: 2026-02-11 02:00 JST
- **変更種別**: モデルアップグレード（性能改善）
- **影響**: chronos-caller Lambda、SageMaker Endpoint

### 動機

ThrottlingException修正の調査中にChronos-2の存在を発見。Chronos-T5-Base (200M) に比べて:
- **250倍高速**: サンプリング不要の分位数直接出力
- **10%高精度**: WQL (Weighted Quantile Loss) で改善
- **40%軽量**: 200M → 120M パラメータ

### 変更内容

| ファイル | 変更内容 |
|----------|----------|
| `scripts/deploy_sagemaker_chronos.py` | Chronos-2用にフルリライト: DLC 2.6.0 + predict_quantiles API |
| `services/chronos-caller/handler.py` | NUM_SAMPLES廃止、モデル名更新、q10/q90追加 |
| `terraform/lambda.tf` | 関数説明を "Chronos-2 AI予測" に更新 |

### SageMaker 構成変更

| 項目 | 旧 (T5-Base) | 新 (Chronos-2) |
|------|-------------|----------------|
| Model Name | `eth-trading-chronos-base` | `eth-trading-chronos-2` |
| Endpoint Config | `eth-trading-chronos-base-config-v2` | `eth-trading-chronos-2-config` |
| DLC Image | `huggingface-pytorch-inference:2.1.0-transformers4.37.0-cpu-py310` | `huggingface-pytorch-inference:2.6.0-transformers4.49.0-cpu-py312` |
| HF_MODEL_ID | `amazon/chronos-t5-base` | `amazon/chronos-2` |
| 依存 | `chronos-forecasting==1.3.0` | `chronos-forecasting>=2.2.0` |
| S3 Key | `chronos-base/model.tar.gz` | `chronos-2/model.tar.gz` |
| 推論方式 | 50回サンプリング → median/std | predict_quantiles → q10/q50/q90 直接出力 |

### デプロイ時の注意点

1. **DLC イメージバージョン**: `chronos-forecasting>=2.2.0` は `torch>=2.2` を要求するため、DLC を 2.6.0 に更新が必要
2. **predict API**: `predict()` は `list[Tensor]` を返す（各要素 shape: `(variates, quantiles, time)`）
3. **predict_quantiles API**: `tuple[list[Tensor], list[Tensor]]` = `(quantiles, mean)` を返す
4. **3D入力**: Chronos-2 は `(batch, variates, time)` の3Dテンソルが必要（旧版は2D）
5. **Endpoint Name**: 既存のまま `eth-trading-chronos-base` を維持（handler.py/terraform変更不要）

---

## 9. 02/12 TP/トレーリングストップ矛盾（デッドコード）

### 概要

position-monitor の処理順序が「SL → **TP** → トレーリングストップ」だった。  
TP=+10% はトレーリングストップの上位ティア（+8%/+12%）より**先**に判定されるため、  
含み益が +10% に達すると必ず TP 利確され、トレーリングストップの高ティアは**デッドコード**だった。

### 影響

- トレーリングストップの 8%+/12%+ ティア（利益を最大限に伸ばす核心機能）が一度も発動しない
- 大きなトレンドに乗っても +10% で頭打ち。Phase 2 (#5) で実装したトレーリングストップが実質無効

### 根本原因

```python
# 旧コード (position-monitor/handler.py)
# 1. SL判定
# 2. TP判定 ← ここで +10% 利確 → return
# 3. トレーリングストップ ← 到達しない
```

### 修正

1. **order-executor**: `take_profit = rate * 1.10` → `rate * 1.30` (安全弁のみ)
2. **position-monitor**: TP判定をトレーリングストップ処理の**後**に移動

```python
# 新コード
# 1. SL判定
# 2. トレーリングストップ (ピーク追跡 + SL引き上げ) ← 利益を伸ばす
# 3. TP判定 (+30% 安全弁) ← 最後のフォールバック
```

### 教訓

- **処理順序は仕様を決める**: 同じコードでも判定順序で機能が死ぬ
- **テスト不足**: +10% 以上の含み益シナリオのテストがなかった

---

## 10. 02/12 DynamoDB Limit+FilterExpression アンチパターン

### 概要

DynamoDB の `Query` で `Limit=1` と `FilterExpression` を同時使用していた。  
DynamoDB は **Limit を FilterExpression の前に適用する**ため、最新の1件が `closed` だと、  
その裏にある `active` なポジションを見逃す。

### 影響

- **position-monitor**: アクティブポジションのSL/TP監視が抜け落ちる可能性
- **order-executor**: 重複ポジション検出が失敗し、二重エントリーの可能性
- **aggregator**: アクティブポジション検索が不完全でHOLD判断が正しくない可能性

### 修正箇所

| ファイル | 関数 | 修正 |
|----------|------|------|
| `position-monitor/handler.py` | `get_active_position()` | Limit=1→5、FilterExpression削除、Python側フィルタ |
| `order-executor/handler.py` | `get_position()` | Limit=1→5、Python側フィルタ |
| `order-executor/handler.py` | `check_any_other_position()` | Limit=1→5、Python側フィルタ |
| `aggregator/handler.py` | `find_all_active_positions()` | Limit=1→5、Python側フィルタ |

### 教訓

- **DynamoDB の Limit は SQL の LIMIT と異なる**: FilterExpression の前に pageSize として働く
- FilterExpression でフィルタしたい場合は十分大きな Limit を指定し、クライアント側でフィルタリング

---

## 11. 02/12 Bedrock nova-micro オンデマンド呼び出し廃止

### 発生日時

2026-02-12 00:18〜00:48（news-collector 30分サイクルで連続エラー）

### エラー

```
botocore.errorfactory.ValidationException: An error occurred (ValidationException)
when calling the Converse operation: Invocation of model ID amazon.nova-micro-v1:0
with on-demand throughput isn't supported. Retry your request with the ID or ARN
of an inference profile that contains this model.
```

### 原因

AWSがBedrock基盤モデルの直接モデルID（`amazon.nova-micro-v1:0`）でのオンデマンド呼び出しを廃止。
**推論プロファイルID**（`us.amazon.nova-micro-v1:0`）経由のみサポートする仕様に変更された。

### 影響

- `news-collector` のLLMセンチメント分析が全て失敗
- フォールバック（ルールベースNLP）で動作は継続していたが、センチメント精度が低下

### 修正箇所

| ファイル | 修正 |
|----------|------|
| `terraform/lambda.tf` | `BEDROCK_MODEL_ID` を `amazon.nova-micro-v1:0` → `us.amazon.nova-micro-v1:0` に変更 |
| `services/news-collector/handler.py` | デフォルト値を同様に変更 |
| `terraform/iam.tf` | IAMポリシーに推論プロファイル用ARN (`arn:aws:bedrock:...:inference-profile/us.amazon.nova-*`) を追加 |

### 教訓

- AWS Bedrockの基盤モデル呼び出しは推論プロファイルID経由が必須になった
- IAMポリシーも `foundation-model/*` だけでなく `inference-profile/*` のリソースARNが必要
