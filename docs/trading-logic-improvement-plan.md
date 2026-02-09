# 取引ロジック改善企画書

**作成日**: 2025-02-09
**ステータス**: Phase 1 完了 → Phase 2 完了 → Phase 3 完了

---

## 📊 現状パフォーマンス分析

### 実績 (43件のクローズ済取引、およその半数がentry_priceバグ影響)

> ⚠️ **注意**: DynamoDB上のentry_price/amountはバグで膨張しており、
> `(exit-entry)*amount` のP/L計算は信頼できない。実際のP/Lは口座残高変動から算出。

| 指標 | 値 |
|------|-----|
| 開始資金 | **¥130,000** |
| 現在残高 | **¥約100,000** |
| 実損失 | **¥約30,000** |
| 勝率 | **9.3%** (4勝/39敗) |
| プロフィットファクター | **0.27** |
| バグ起因 (entry_price膨張, 修正済) | 約26件影響 |

### 致命的パターン

1. **即時売り**: 43件中41件が **保有5分以内で即売り**
2. **XRP偏重**: 15件/43件 = 35%がXRP
3. **SL/TP未発動**: 全取引がシグナル売り。SL/TPは一度も発動していない
4. **BUY→即SELL ループ**: BUYして次サイクルですぐSELLシグナル → 往復ビンタ

### シグナル統計 (810シグナル)

- BUY: 281回 (35%), SELL: 234回 (29%), HOLD: 295回 (36%)
- BUYシグナル平均スコア: +0.309
- コンポーネント平均: Technical=+0.436, Chronos=+0.248, Sentiment=+0.090
- BUY閾値(0.20)超え → 発火率35%は高すぎる

---

## 🔧 改善項目一覧 (優先度順)

### 【P1: 即効性・高インパクト】

#### 1. MACDシグナルラインの修正 ✅ 実装済
- **現状**: `signal_line = macd_line * 0.9` — 偽のシグナルライン。MACD方向と常に一致
- **問題**: EMAスプレッドチェックと同等。クロスオーバー検知不可
- **修正**: MACD系列全体を計算し、そのEMA(9)で正しいシグナルラインを算出
- **影響**: テクニカル重み0.45 × MACD0.25 = 全体の11%に影響
- **実装先**: `services/technical/handler.py`

#### 2. 最低保有時間 (Minimum Hold Period) ✅ 実装済
- **現状**: BUY→ 5分後にSELLシグナル→即損切り。43件中41件がこのパターン
- **修正**: BUYから最低30分は通常SELLシグナルを無視（SL/TPのみ有効）
- **影響**: 即時往復ビンタの防止
- **実装先**: `services/aggregator/handler.py`

#### 3. ボリンジャーバンド デッドゾーン解消 ✅ 実装済
- **現状**: BB position 0.2〜0.8の60%範囲がスコア0
- **修正**: 線形スコア `(0.5 - bb_position) * 2 * w_bb` で全範囲グラデーション化。バンド外は1.2倍ボーナス
- **影響**: テクニカルスコアの精度向上
- **実装先**: `services/technical/handler.py`

### 【P2: 中インパクト・ロジック改善】

#### 4. Chronos スコアスケール調整 ✅ 実装済
- **現状**: ±5%変動 = ±1.0だが、1時間で5%は稀。Chronosスコアは常にほぼ0
- **修正**: ±1%変動 = ±1.0 に変更
- **影響**: 40%ウェイトのChronosが実質的に機能開始
- **実装先**: `services/chronos-caller/handler.py`

#### 5. トレーリングストップ実装 ✅ 実装済
- **現状**: SL=-5%, TP=+10%で固定。+9%の含み益が反転→SL発動で損失
- **修正**: 含み益+3%でSL=建値、+5%でSL=+3%、+8%でSL=+6%。DynamoDB永続化+Slack通知
- **影響**: 利益確保率向上
- **実装先**: `services/position-monitor/handler.py`

#### 6. XRP偏重防止（通貨分散ルール） ✅ 実装済
- **現状**: XRP 35%集中
- **修正**: 同一通貨の最大保有数制限 MAX_POSITIONS_PER_PAIR=1（環境変数で変更可）
- **実装先**: `services/aggregator/handler.py`

### 【P3: 精度向上・システム改善】

#### 7. 市場レジーム検知 ✅ 実装済
- **現状**: トレンド相場もレンジ相場も同じロジック
- **修正**: ADXで相場状態判定。ADX>25:トレンド(MACD/SMA重視)、ADX<20:レンジ(RSI/BB重視)
- **実装先**: `services/technical/handler.py`

#### 8. テクニカル指標拡充 ✅ 実装済
- **追加済**: ADX (トレンド強度)、ATR (ボラティリティ), ATR% (価格対比)、レジーム判定
- **実装先**: `services/technical/handler.py`

#### 9. BUY閾値引き上げ ✅ 実装済
- **現状**: BUY閾値デフォルト0.20 → 発火率35%(高すぎ)
- **修正**: デフォルト0.30に引き上げ。高確信度の取引のみ実行
- **実装先**: `services/aggregator/handler.py`

#### 10. ドローダウン制御 (サーキットブレーカー) ✅ 実装済
- **現状**: 損失が続いても止まらない
- **修正**: 日次累計損失閾値 (¥15,000) or 連敗回数 (5回) でBUY停止。トリップ後6時間冷却
- **ON/OFF**: `CIRCUIT_BREAKER_ENABLED` 環境変数で切替（デフォルト: OFF）
- **その他環境変数**: `CB_DAILY_LOSS_LIMIT_JPY`, `CB_MAX_CONSECUTIVE_LOSSES`, `CB_COOLDOWN_HOURS`
- **実装先**: `services/order-executor/handler.py`

---

## 推奨実装順序

```
2(最低保有時間) → 1(MACD修正) → 4(Chronosスケール) → 9(BUY閾値) 
→ 3(BBデッドゾーン) → 5(トレーリングストップ) → 6(通貨分散) 
→ 10(サーキットブレーカー) → 7(レジーム検知) → 8(指標拡充)
```

---

## 実装ログ

| 日付 | 項目 | コミット | 備考 |
|------|------|---------|------|
| 2025-02-09 | #2 最低保有時間 | 8e582d0 | 30分ホールドルール |
| 2025-02-09 | #1 MACD修正 | - | EMA(9)シグナルライン、クロスオーバー検知可能に |
| 2025-02-09 | #4 Chronosスケール | 95ef463 | ±5%→±1%、Chronosが実質機能化 |
| 2025-02-09 | #9 BUY閾値 | - | 0.20→0.30、発火率削減 |
| 2025-02-09 | #3 BBデッドゾーン | - | 線形グラデーション+バンド外ボーナス |
| 2025-02-09 | #5 トレーリングストップ | - | +3%/+5%/+8%段階式、DB永続化 |
| 2025-02-09 | #6 通貨分散 | - | MAX_POSITIONS_PER_PAIR=1 |
| 2025-02-09 | #7 レジーム検知 | - | ADX判定、ウェイト動的変更 |
| 2025-02-09 | #8 指標拡充 | - | ADX/ATR/ATR%/レジーム追加 |
| 2025-02-09 | #10 サーキットブレーカー | - | 日次損失/連敗制御、デフォルトOFF |

---

## 🔮 Phase 2 改善項目 (全項目実装済)

> Phase 1 (#1-#10) 完了後のステップ。2026-02-09 に全8項目を実装・デプロイ。

### 【A. AI分析の精度に直結する問題（優先度高）】

#### 11. OHLCデータの保存・活用 ✅ 実装済
- **改善内容**: Binance klines API から OHLCV (open/high/low/close/volume) を取得し DynamoDB に保存
- **ATR**: 正式な True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|) を使用。OHLC がない古いレコードは従来の close 近似にフォールバック
- **ADX**: +DM = max(High[i]-High[i-1], 0)、-DM = max(Low[i-1]-Low[i], 0) で正式計算
- **影響範囲**: `services/price-collector/handler.py`, `services/technical/handler.py`

#### 12. MACDスコアリングのグラデーション化 ✅ 実装済
- **改善内容**: ヒストグラム振幅ベースのグラデーションスコアに変更
- **ロジック**: `norm_hist = histogram / current_price * 100` (価格比%)、`hist_score = clamp(norm_hist / 0.1, -1, 1)`
- ±0.1% のヒストグラムで ±1.0 にスケール（5分足の典型値）
- **影響範囲**: `services/technical/handler.py`

#### 13. RSI計算のWilder's方式への修正 ✅ 実装済
- **改善内容**: Wilder's smoothed moving average に修正
- **ロジック**: 最初の period 本は SMA で初期化、以降は `avg = (avg * (period-1) + current) / period` で指数平滑化
- 全データを使用（従来は直近 N 期間のみ）
- **影響範囲**: `services/technical/handler.py`

#### 14. 出来高（Volume）データの活用 ✅ 実装済
- **改善内容**: `calculate_volume_signal()` 関数を追加。出来高急増時にテクニカルスコアを増幅
- **ロジック**: 直近20本の平均出来高と現在の出来高を比較。ratio > 1.5 で増幅開始、ratio 2.5 で上限 1.3x に到達。平均以下は 1.0 (減衰なし)
- **影響範囲**: `services/price-collector/handler.py`, `services/technical/handler.py`

### 【B. モデル・インフラの改善（優先度中）】

#### 15. Chronos-T5-Tiny のウェイト調整 ✅ 実装済 (短期+長期)
- **データ分析**: 810シグナルの相関分析を実施
  - Chronos near-zero率: 51.9% (スコアがほぼ0のシグナルが半数以上)
  - 分散比: Technical=0.57, Chronos=0.35, Sentiment=0.08
- **短期 ✅**: Technical 0.45→**0.55**, Chronos 0.40→**0.30**, Sentiment 0.15 (据置)
- **中期 ⏭️ スキップ**: Chronos-T5-Small 切替はコスト増(4x memory)に対し効果が薄いため見送り。モデルサイズよりも入力データの改善 (Typical Price) が効果的
- **長期 ✅**: 810シグナルの実データで分散比を測定し、データドリブンでウェイトを決定
- **追加改善**: Chronos 入力を close → **Typical Price (H+L+C)/3** に変更。ローソク足の重心を使うことで値動き情報を豊かに
- **影響範囲**: `services/aggregator/handler.py`, `services/chronos-caller/handler.py`

#### 16. decoder_with_past (KVキャッシュ) の活用 ✅ 実装済
- **改善内容**: `decoder_with_past_model.onnx` を使った KV-Cache 付き高速デコードを実装
- **ロジック**: 初回ステップはフルデコーダ実行 → present KV を抽出。2ステップ目以降は最後の1トークンのみ入力し past_key_values で高速デコード
- **効果**: O(n²) → O(n) で推論時間が大幅短縮（特にサンプル数20の反復が高速化）
- **影響範囲**: `services/chronos-caller/handler.py`

#### 17. センチメント分析の高度化 ✅ 実装済
- **改善内容**: ルールベース NLP を大幅強化（FinBERT は Lambda サイズ制約とコスト面で見送り）
- **3段階の強度**: strong (±0.25) / moderate (±0.15) / mild (±0.06) で重み分け
- **否定語検出**: not, no, never, n't, without, fails 等 → 直前3語以内で極性反転
- **バイグラム/フレーズ**: all-time high, death cross, etf approved, rug pull 等20+フレーズ
- **暗号通貨特化語彙**: halving, whale accumulation, delisted, flash crash 等
- **スコア範囲**: ±0.4 (旧: ±0.3) まで拡大
- **影響範囲**: `services/news-collector/handler.py`

### 【C. 運用・安全性の改善（優先度中〜低）】

#### 18. トレード記録への分析コンテキスト保存 ✅ 実装済
- **改善内容**: `analysis_context` を SQS メッセージに含め、trades テーブルに保存
- **保存項目**: technical_score, chronos_score, sentiment_score, weight_technical/chronos/sentiment, buy_threshold, sell_threshold
- **データフロー**: aggregator → SQS (analysis_context付き) → order-executor → DynamoDB trades テーブル
- **影響範囲**: `services/aggregator/handler.py`, `services/order-executor/handler.py`

### 【Phase 3: 運用データフィードバック】

#### 19. VOL_CLAMP_MIN 引き上げ（最低BUY閾値の底上げ） ✅ 実装済
- **問題**: Phase 2 初日の3トレード全てが `BUY閾値=0.15`（クランプ下限）で発火。BTC(0.159)/AVAX(0.165)は閾値をわずか0.009/0.015超えただけの限界的シグナル
- **根本原因**: `VOL_CLAMP_MIN=0.5` → `BASE_BUY_THRESHOLD(0.30) × 0.5 = 0.15` が低すぎる。Tech単独の強い反発が他2コンポーネント(AI/Sent)の反対意見を無視してBUY発動
- **修正**: `VOL_CLAMP_MIN` を 0.5 → **0.67** に引き上げ（最低BUY閾値 0.15→**0.20**、最低SELL閾値 -0.10→**-0.134**）
- **効果**: BTC/AVAXの限界的トレード(計¥313損失)を阻止。XRP(0.222>0.20)は閾値超えで正当なシグナルとして引き続き発動可能
- **影響範囲**: `services/aggregator/handler.py`

#### 20. Market Context 第4の柱 ✅ 実装済
- **問題**: 3成分体制ではマクロ市場環境（恋怐/強欲、レバレッジ偏り、BTC支配力）を考慮できない
- **新規Lambda**: `market-context` (30分間隔 EventBridge)
  - Fear & Greed Index (Alternative.me) — 市場の恐怖/強欲度
  - Funding Rate (Binance Futures) — レバレッジポジションの偏り
  - BTC Dominance (CoinGecko) — 資金フロー方向
- **Aggregator更新**: ウェイトを Tech=0.45, Chronos=0.25, Sent=0.15, MktCtx=0.15 に変更
- **BTC Dominance補正**: アルトコインにBTC Dom >60%で-0.05、<40%で+0.05
- **データ鮮度チェック**: 2時間以上古い場合は中立(0.0)扱い
- **影響範囲**: `services/market-context/handler.py`(新規), `services/aggregator/handler.py`, Terraform全体

#### 20a. 4成分化に伴う閾値調整 ✅ 実装済
- **問題**: 4成分に分散したことでスコアが約15%圧縮 + Market Context の上方バイアス(+0.02)
- **修正**: BUY閾値 0.30→0.28、SELL閾値 -0.20→-0.15
- **検証**: 1,578件のシグナルデータで旧3成分と同等のシグナル頻度を維持する閾値を算出
- **影響範囲**: `services/aggregator/handler.py`

#### 17a. NLP「buy the dip」コンテキスト修正 ✅ 実装済
- **問題**: 「buy the dip」「whales accumulate」等の逆張りフレーズで、「buy」がBullish、「dip」がBearishと個別検出され相殺していた
- **修正**: バイグラム/フレーズマッチングを個別キーワードより先に処理し、使用済み単語をスキップ
- **影響範囲**: `services/news-collector/handler.py`

### Phase 2 実装ログ

| 日付 | 項目 | コミット | 備考 |
|------|------|---------|------|
| 2026-02-09 | #12 MACD グラデーション | `8211ac1` | ヒストグラム振幅ベース |
| 2026-02-09 | #13 RSI Wilder's | `8211ac1` | 指数平滑化 |
| 2026-02-09 | #15 Chronos ウェイト | `8211ac1` | 810信号分析→Tech=0.55,Chronos=0.30 |
| 2026-02-09 | #18 トレード記録 | `8211ac1` | analysis_context 保存 |
| 2026-02-09 | #11 OHLC 保存 | `8211ac1` | Binance klines→DynamoDB |
| 2026-02-09 | #14 Volume 活用 | `8211ac1` | volume_multiplier 1.0-1.3 |
| 2026-02-09 | #16 KV キャッシュ | `8211ac1` | decoder_with_past 高速デコード |
| 2026-02-09 | #17 NLP センチメント | `8211ac1` | 3段階強度+否定語+バイグラム |
| 2026-02-09 | Typical Price | `90684dc` | Chronos入力を(H+L+C)/3に変更 |
| 2026-02-09 | #19 VOL_CLAMP_MIN | `5ffcbea` | 0.5→0.67, 最低BUY閾値 0.15→0.20 |
| 2026-02-10 | #17a NLP buy the dip | `aa138cf` | コンテキスト認識、使用済み単語スキップ |
| 2026-02-10 | #20 Market Context | `72cf12f` | 第4の柱: F&G + Funding + BTC Dom, Tech=0.45/Chronos=0.25/Sent=0.15/Mkt=0.15 |
| 2026-02-10 | #20a 閾値調整 | `8b5f2a4` | BUY 0.30→0.28, SELL -0.20→-0.15, 4成分圧縮補正 |
