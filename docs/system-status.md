# システム現状ステータス — Phase 7 品質改善チェックポイント

**スナップショット日時**: 2026-02-12 JST  
**最新コミット**: `#NEXT` (main) — Phase 7 コード品質・信頼性改善  
**目的**: Phase 7 の変更内容を記録。

---

## 📌 Phase 7 変更サマリ (7項目)

| # | 改善内容 | 対象ファイル | 重要度 |
|---|---------|-------------|--------|
| 32 | TP/トレーリングストップ矛盾修正 (TP +10%→+30%) | order-executor, position-monitor | 🔴 致命的 |
| 33 | 負Kelly時トレードスキップ | order-executor | 🔴 致命的 |
| 34 | DynamoDB Limit+FilterExpression修正 | position-monitor, order-executor, aggregator | 🟠 高 |
| 35 | alt_dominance_adjustment スコア範囲クランプ | aggregator | 🟡 中 |
| 36 | bare except修正 + ログ追加 | aggregator, order-executor | 🟡 中 |
| 37 | BUY時スプレッドチェック追加 | order-executor | 🟠 高 |
| 38 | Chronos信頼度フィルター (低確信度減衰) | aggregator | 🟠 高 |

---

## 📋 Phase 7 変更詳細

### 32. TP/トレーリングストップ矛盾修正

**問題**: 固定TP(+10%)がposition-monitorでトレーリングストップ処理**前に**判定されていた。  
→ トレーリングストップの8%+/12%+ティア（利益を伸ばす核心機能）が**デッドコード**。

**修正**:
- `order-executor`: `save_position`の`take_profit`を`rate * 1.10` → `rate * 1.30`に変更
- `position-monitor`: TP判定をトレーリングストップ処理の**後**に移動
  - SL判定 → トレーリングストップ（ピーク追跡+SL引き上げ） → TP判定（+30%安全弁）

**効果**: トレーリングストップの全ティア(3-5%/5-8%/8-12%/12%+)が正常に機能。大きなトレンドに乗った場合に利益を最大化できる。

### 33. 負Kelly時トレードスキップ

**問題**: Kelly Criterionが負（期待値マイナス = エッジなし）でも`KELLY_MIN_FRACTION=0.10`で10%ベットしていた。

**修正**: `kelly_full <= 0`の場合、`return 0`でトレードをスキップ。

**効果**: 統計的にエッジがないと判明した場合は自動的にトレードを停止。資金を無意味に消耗しない。

### 34. DynamoDB Limit+FilterExpression修正

**問題**: DynamoDBの`Limit`は`FilterExpression`**前に**適用される。`Limit=1`だと、最新のpositionがclosedの場合、その裏のactiveなpositionを見逃す。

**修正**: 3ファイル4箇所で`Limit=1`→`Limit=5`に変更し、Python側でフィルタリング。
- `position-monitor/handler.py` `get_active_position()`
- `order-executor/handler.py` `get_position()`, `check_any_other_position()`
- `aggregator/handler.py` `find_all_active_positions()`

### 35. alt_dominance_adjustment スコア範囲クランプ

**問題**: BTC Dominance補正(±0.05)が加重平均の**外で**加算され、`total_score`が[-1, 1]を超えうる。

**修正**: `total_score = max(-1.0, min(1.0, total_score))` でクランプ。

### 36. bare except修正 + ログ追加

**修正箇所**:
- `aggregator/handler.py` `extract_score()`: `except:` → `except (json.JSONDecodeError, TypeError, ValueError) as e:` + ログ
- `order-executor/handler.py` `get_api_credentials()`: `except:` → `except Exception as e:` + ログ

### 37. BUY時スプレッドチェック追加

**問題**: 板が薄い通貨（AVAX等）で成行注文が大きく滑り、エントリーから7秒でSL到達する事象が発生。

**修正**: `execute_buy()`冒頭でCoincheck ticker APIのbid/askスプレッドを確認。`MAX_SPREAD_PCT`（デフォルト1.0%）を超える場合はBUYをスキップ。

**環境変数**: `MAX_SPREAD_PCT` (デフォルト: `1.0`)

### 38. Chronos信頼度フィルター (低確信度減衰)

**問題**: Chronos confidence < 0.3 の低品質予測がそのままスコアに反映され、ノイズシグナルの原因に。

**修正**: `aggregator/handler.py` `score_pair()`で信頼度フィルター追加。
- `confidence < 0.3` → スコアを `confidence / 0.3` 倍に減衰
- `confidence >= 0.3` → 変更なし

---

## 🔧 現在の設定値一覧

### Aggregator

| パラメータ | 環境変数 | 現在値 | 備考 |
|-----------|---------|--------|------|
| テクニカル重み | `TECHNICAL_WEIGHT` | 0.35 | Phase 4: AI均等化 |
| Chronos重み | `AI_PREDICTION_WEIGHT` | 0.35 | Phase 4: AI均等化 |
| センチメント重み | `SENTIMENT_WEIGHT` | 0.15 | |
| マーケットCtx重み | `MARKET_CONTEXT_WEIGHT` | 0.15 | |
| BUY基準閾値 | `BASE_BUY_THRESHOLD` | 0.25 | |
| SELL基準閾値 | `BASE_SELL_THRESHOLD` | -0.13 | |
| ボラ補正下限 | `VOL_CLAMP_MIN` | 0.67 | |
| 最低保有時間 | `MIN_HOLD_SECONDS` | 1800 | 30分 |
| 通貨最大同時保有 | `MAX_POSITIONS_PER_PAIR` | 1 | |
| F&G恐怖閾値 | - | 20 | |
| F&G強欲閾値 | - | 80 | |
| F&G Fear BUY倍率 | - | ×1.35 | |
| F&G Greed BUY倍率 | - | ×1.20 | |
| Chronos最低信頼度 | - | 0.30 | **Phase 7 新規** |

### Order Executor

| パラメータ | 環境変数 | 現在値 | 備考 |
|-----------|---------|--------|------|
| 最大ポジション額 | `MAX_POSITION_JPY` | 15,000 | |
| 予備資金 | `RESERVE_JPY` | 1,000 | |
| 最小注文額 | `MIN_ORDER_JPY` | 500 | |
| スプレッド上限 | `MAX_SPREAD_PCT` | 1.0% | **Phase 7 新規** |
| サーキットブレーカー | `CIRCUIT_BREAKER_ENABLED` | false | |
| 負Kelly時 | - | スキップ | **Phase 7 変更** (旧: 10%ベット) |

### Position Monitor

| 機能 | 現在値 | 備考 |
|------|--------|------|
| SL | -5% (固定初期値) | |
| TP | **+30%** (安全弁) | **Phase 7 変更** (旧: +10%) |
| トレーリングストップ | 3-5%: trail 2.0%, 5-8%: 1.5%, 8-12%: 1.2%, 12%+: 1.0% | |
| TP判定タイミング | トレーリングストップ処理の**後** | **Phase 7 変更** |

### SageMaker構成

| 項目 | 値 |
|------|-----|
| エンドポイント名 | `eth-trading-chronos-base` |
| タイプ | Serverless (6144MB, max_concurrency=8) |
| モデル | Chronos-2 (120M), Chronos-T5-Base (200M) |
| DLC Image | `huggingface-pytorch-inference:2.1.0-transformers4.37.0-cpu-py310` |

---

## ✅ 全改善実装履歴 (#1-#38)

### Phase 1 (10項目)

| # | 改善内容 |
|---|---------|
| 1 | MACDシグナルライン EMA(9) 修正 |
| 2 | 最低保有時間 30分 |
| 3 | BB線形グラデーション化 |
| 4 | Chronosスコアスケール ±5%→±1% |
| 5 | トレーリングストップ 3段階 |
| 6 | 通貨分散 MAX_POSITIONS_PER_PAIR=1 |
| 7 | ADXレジーム検知 |
| 8 | テクニカル指標拡充 (ADX/ATR/ATR%) |
| 9 | BUY閾値 0.20→0.30 |
| 10 | サーキットブレーカー (デフォルトOFF) |

### Phase 2 (8+1項目)

| # | 改善内容 |
|---|---------|
| 11 | OHLCVデータ保存・ATR/ADX正式化 |
| 12 | MACDヒストグラムグラデーション |
| 13 | RSI Wilder's指数平滑化 |
| 14 | Volumeシグナル (1.0-1.3x乗数) |
| 15 | ウェイトデータドリブン |
| 16 | KVキャッシュデコード |
| 17 | NLPセンチメント高度化 |
| 18 | トレードコンテキスト保存 |
| - | Chronos Typical Price (H+L+C)/3 |

### Phase 3 (運用データフィードバック)

| # | 改善内容 |
|---|---------|
| 19 | VOL_CLAMP_MIN 0.5→0.67 |
| 17a | NLP "buy the dip" コンテキスト修正 |
| 20 | Market Context 第4の柱 |
| 20a | 4成分化閾値調整 |

### Phase 4/4.5 (Self-Improving Pipeline + Data Quality)

| # | 改善内容 |
|---|---------|
| 21 | Daily Reporter Lambda |
| 22 | Auto-Improve Pipeline |
| 23 | Trades テーブル TTL 90日 |
| 24 | データ品質ゲート |
| 25 | Auto-Improve Pre-Check ゲート |
| 26 | F&G連動BUY閾値抑制 |
| 27 | ゴミデータクリーンアップ |

### Phase 5 (SELL判断改善)

| # | 改善内容 |
|---|---------|
| 28 | 連続トレーリングストップ |
| 29 | モメンタム減速検知 |

### Phase 6 (AI/Chronosアップグレード)

| # | 改善内容 |
|---|---------|
| 30 | Chronos SageMaker Serverless化 |
| 31 | 確信度ベース動的Chronosウェイト |

### Phase 7 (コード品質・信頼性改善) ← **今回**

| # | 改善内容 |
|---|---------|
| 32 | TP/トレーリングストップ矛盾修正 (TP +10%→+30%) |
| 33 | 負Kelly時トレードスキップ |
| 34 | DynamoDB Limit+FilterExpression修正 |
| 35 | alt_dominance_adjustment スコア範囲クランプ |
| 36 | bare except修正 + ログ追加 |
| 37 | BUY時スプレッドチェック追加 |
| 38 | Chronos信頼度フィルター |
