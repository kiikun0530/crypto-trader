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
