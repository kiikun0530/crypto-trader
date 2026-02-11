# 取引ロジック改善企画書

**作成日**: 2025-02-09
**ステータス**: Phase 1 完了 → Phase 2 完了 → Phase 3 完了 → Phase 4 完了 → Phase 5 完了 → Phase 6 完了 → Phase 7 完了

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

### 【Phase 4: Self-Improving Pipeline】

#### 21. Daily Reporter Lambda ✅ 実装済
- **目的**: 日次トレードデータの自動集計とパフォーマンスレポート生成
- **実装**: EventBridge (23:00 JST) → daily-reporter Lambda
  - 全通貨の24h/7d/30d取引履歴を集計
  - シグナル統計（コンポーネント別near_zero率含む）
  - S3にJSON保存 (90日ライフサイクル) + Slack通知
- **影響範囲**: `services/daily-reporter/handler.py`(新規), Terraform (dynamodb, lambda, eventbridge, iam, s3)

#### 22. Self-Improving Pipeline (Auto-Improve) ✅ 実装済
- **目的**: AIが日次データを分析し、アルゴリズムを自動改善
- **実装**: daily-reporter → `repository_dispatch` → `auto-improve.yml` GitHub Actions
  - Claude Sonnet がレポート + ソースコードを分析
  - NO_ACTION / PARAM_TUNE / CODE_CHANGE を判定
  - PARAM_TUNE/CODE_CHANGE: search/replace でコード変更 → 構文チェック → デプロイ → docs更新 → git push
  - DynamoDB `improvements` テーブルに改善記録 (TTL: 180日)
- **安全ルール**: ウェイト±0.05/回、閾値±0.03/回、2週間以内の変更は効果測定のため抑止
- **影響範囲**: `.github/workflows/auto-improve.yml`(新規), Terraform

#### 23. Trades テーブル TTL 追加 ✅ 実装済
- **目的**: 取引履歴の自動クリーンアップ
- **修正**: trades テーブルに TTL (90日) を追加。order-executor の save_trade に `ttl` フィールド追加
- **影響範囲**: `services/order-executor/handler.py`, `terraform/dynamodb.tf`

### 【Phase 4.5: Data Quality & Noise Protection】

#### 24. データ品質ゲート (Daily Reporter) ✅ 実装済
- **目的**: 自動改善パイプラインが低品質データで誤った判断をするのを防止
- **実装**: `build_data_quality()` 関数を追加
  - Wilson信頼区間 (95%) で勝率の下限/上限を算出
  - 最低取引数チェック (3件未満は改善トリガー無効)
  - クールダウンチェック (直近2週間の改善履歴)
  - `data_quality.allow_improvement` が `false` の場合、`trigger_auto_improve()` をスキップ
- **影響範囲**: `services/daily-reporter/handler.py`

#### 25. Auto-Improve Pre-Check ゲート ✅ 実装済
- **目的**: GitHub Actions側でもハードコードされたゲートで不適切な改善を防止
- **実装**: `auto-improve.yml` に pre-check ステップ追加
  - `total_trades < 3` → 即座にNO_ACTION
  - `confidence_score` 要件: PARAM_TUNE ≥ 0.5, CODE_CHANGE ≥ 0.6
- **影響範囲**: `.github/workflows/auto-improve.yml`

#### 26. Fear & Greed連動 BUY閾値抑制 ✅ 実装済
- **問題**: Extreme Fear (F&G≤20) 時にChronos AIが異常に高いスコア (AI=+1.000) を出し、損失発生
  - 実例: 02/10 ETH (-¥1,661, -1.89%), XRP (-¥1,501, -2.32%) — F&G=14の市場で
- **修正**: `calculate_dynamic_thresholds()` にF&G補正ロジック追加
  - F&G ≤ 20 (Extreme Fear): BUY閾値 ×1.35 (より慎重に)
  - F&G ≥ 80 (Extreme Greed): BUY閾値 ×1.20 (バブル警戒)
  - SELL閾値は変更なし (ストップロスは常に実行)
- **新定数**: `FNG_FEAR_THRESHOLD=20`, `FNG_GREED_THRESHOLD=80`, `FNG_BUY_MULTIPLIER_FEAR=1.35`, `FNG_BUY_MULTIPLIER_GREED=1.20`
- **Slack通知**: F&G補正適用時は `⚠️ F&G=14: BUY_TH ×1.35` 等の警告を表示
- **影響範囲**: `services/aggregator/handler.py`

#### 27. ゴミデータクリーンアップ ✅ 完了
- **問題**: 02/09のCoincheck API fillバグ (order_idフィルタ未使用) により、異常なentry_priceのレコードが発生
  - trades テーブル: 101件のゴミレコード (score=0, threshold=0, BTC rate=520M JPY vs 実勢14M)
  - positions テーブル: 39件のゴミポジション (全closed, 異常なentry_price)
- **対応**: 手動スクリプトで全ゴミレコードを削除
  - trades: 101件削除、12件の正常レコードが残存
  - positions: 39件削除、19件の正常レコードが残存 (1 open: sol_jpy)
- **再発防止**: fillバグは既に修正済 (`order_id` フィルタ + 50%乖離チェック)

### 【Phase 5: SELL判断改善 — 利確タイミング最適化】

#### 28. 連続トレーリングストップ ✅ 実装済
- **問題**: 旧3段階式トレーリングストップ (+3%/+5%/+8%) は隙間が大きく、2-3%の利益を返してしまう
  - +2.9%→-5%のケースでトレーリング非適用（全額損失）
  - 離散ステップ間の利益が保護されない
- **修正**: ピーク価格追跡 + 連続トレーリングへ全面改修
  - `highest_price` をDynamoDBに永続化し、ポジションの最高到達価格を追跡
  - ピークからの下落率でSLを動的設定（利益水準に応じてトレール幅を変動）
  - 3-5%帯: ピークから2.0%でSL, 5-8%帯: 1.5%, 8-12%帯: 1.2%, 12%+: 1.0%
  - 3%以上到達後は必ず建値以上を保証
- **Slack通知**: ピーク更新 + SL引き上げ時にトレール幅を表示
- **影響範囲**: `services/position-monitor/handler.py`

#### 29. モメンタム減速検知 (MACD histogram slope) ✅ 実装済
- **問題**: 全4成分の遅行指標がSELL閾値に到達するのを待つため、反転の初動を逃す
  - MACDヒストグラムが正→縮小中（上昇モメンタム減速）を検知する仕組みがなかった
- **修正**:
  - `technical/handler.py`: `calculate_macd_histogram_slope()` 関数を追加
    - 直近3本のヒストグラム変化の平均傾きを算出 → -1.0〜+1.0にスケール
    - `macd_histogram_slope` をindicatorsに追加
  - `aggregator/handler.py`: `decide_action()` でモメンタム減速SELL判定
    - ヒストグラム正 + 傾き < -0.3 → SELL閾値を50%緩和（例: -0.10→-0.05）
    - hold period経過後のみ適用（即売り防止）
- **影響範囲**: `services/technical/handler.py`, `services/aggregator/handler.py`

### 【Phase 6: AI/Chronos アップグレード — SageMaker Base化】

#### 30. Chronos SageMaker Serverless Endpoint化 ✅ 実装済
- **問題**: Lambda上のONNX Chronos-T5-Tiny (8M params) は予測精度が不十分
  - 入力60本(5h)→パターン認識が弱い
  - 20サンプル→中央値が不安定
  - ±1%スケール→常にスコア飽和
  - 確信度メトリクスなし
- **修正**: SageMaker Serverless Endpoint + Chronos-T5-Base (200M params) に全面移行
  - **モデル**: Tiny(8M) → Base(200M) — 25倍のパラメータ数
  - **入力**: 60本(5h) → 336本(28h) — 日次サイクル1周+αを捕捉
  - **サンプル**: 20 → 50 — 中央値の安定性向上
  - **スコアスケール**: ±1% → ±3% — 飽和解消
  - **外れ値除去**: ±20%超の予測を現在価格で置換
  - **トレンド加速ボーナス**: 後半予測 > 前半予測 → 最大±0.15加算
  - **std減衰**: CV > 5%でスコア50%減衰
  - **フォールバック**: SageMaker障害時はモメンタムベーススコア (confidence=0.1)
- **SageMaker構成**:
  - エンドポイント: `eth-trading-chronos-base` (Serverless, 6144MB, max_concurrency=2)
  - DLC Image: `huggingface-pytorch-inference:2.1.0-transformers4.37.0-cpu-py310`
  - モデル格納: `s3://eth-trading-sagemaker-models-652679684315/chronos-base/model.tar.gz` (717.7MB)
  - 依存: `chronos-forecasting==1.3.0` (torch 2.1.0互換にピン留め必須)
- **Lambda変更**: memory 1536→256MB, ONNX Runtimeレイヤー不要
- **影響範囲**: `services/chronos-caller/handler.py` (全面書換), `terraform/lambda.tf`, `terraform/iam.tf`

#### 31. 確信度ベース動的Chronosウェイト ✅ 実装済
- **問題**: Chronosウェイトが固定0.25だが、予測確信度が低い時も高い時も同じ影響力
- **修正**: SageMaker推論結果の `confidence` (0.0-1.0) に基づきウェイトを動的変動
  - `weight_shift = (confidence - 0.5) × 0.30` → クランプ [-0.15, +0.10]
  - 高確信度 (1.0): Chronos 0.35, Tech 0.35
  - 中確信度 (0.5): Chronos 0.25, Tech 0.45 (ベース)
  - 低確信度 (0.0): Chronos 0.10, Tech 0.60
  - Sentiment/MktCtx は固定 (0.15/0.15)
- **Slack通知**: 確信度インジケーター (🟢≥0.7, 🟡≥0.4, 🔴<0.4) + 通貨別動的ウェイト表示
- **影響範囲**: `services/aggregator/handler.py`

---

### Phase 7: コード品質・信頼性改善 (AWS分析ベース)

> Phase 7 は AWS 本番環境の DynamoDB・CloudWatch・Lambda を直接分析し、
> 実データ (14件のトレード: 勝率7.1%, P/L -¥14,682) から発掘した構造的バグ・設計問題を修正。

#### 32. TP/トレーリングストップ矛盾修正 ✅ 実装済
- **問題**: position-monitorでTP判定(+10%)がトレーリングストップ処理の**前に**あった
  - → トレーリングストップの8%+/12%+ティアが完全にデッドコード
  - → +9%の含み益は常にTP利確。利益を伸ばせない構造
- **修正**:
  - `order-executor`: save_position の take_profit を `rate * 1.10` → `rate * 1.30` に変更
  - `position-monitor`: TP判定をトレーリングストップの**後**に移動。+30%は安全弁のみ
- **影響範囲**: `services/order-executor/handler.py`, `services/position-monitor/handler.py`

#### 33. 負Kelly時トレードスキップ ✅ 実装済
- **問題**: Kelly Criterionが負(期待値マイナス)でも KELLY_MIN_FRACTION=0.10 で10%ベットしていた
- **修正**: `kelly_full <= 0` なら `return 0` でトレード自体をスキップ
- **影響範囲**: `services/order-executor/handler.py`

#### 34. DynamoDB Limit+FilterExpression修正 ✅ 実装済
- **問題**: DynamoDB の `Limit` は `FilterExpression` の**前に**適用される
  - `Limit=1` だと最新レコードがclosedの場合、その後ろのactiveレコードを見逃す
- **修正**: 3ファイル4箇所で `Limit=1` → `Limit=5` に変更。Python側でフィルタリング
- **影響範囲**: `services/position-monitor/handler.py`, `services/order-executor/handler.py`, `services/aggregator/handler.py`

#### 35. alt_dominance_adjustment スコア範囲クランプ ✅ 実装済
- **問題**: BTC Dominance補正(±0.05)が加重平均の外で加算され、total_scoreが[-1, 1]を逸脱しうる
- **修正**: `total_score = max(-1.0, min(1.0, total_score))` でクランプ
- **影響範囲**: `services/aggregator/handler.py`

#### 36. bare except修正 + ログ追加 ✅ 実装済
- **修正**:
  - `aggregator` extract_score: `except:` → `except (json.JSONDecodeError, TypeError, ValueError) as e:` + ログ
  - `order-executor` get_api_credentials: `except:` → `except Exception as e:` + ログ
- **影響範囲**: `services/aggregator/handler.py`, `services/order-executor/handler.py`

#### 37. BUY時スプレッドチェック追加 ✅ 実装済
- **問題**: 板が薄い通貨(AVAX等)で成行注文が大きく滑り、エントリーから7秒でSL到達
- **修正**: execute_buy冒頭でCoincheck ticker APIのbid/askスプレッドを確認。MAX_SPREAD_PCT(1.0%)超過ならBUYスキップ
- **環境変数**: `MAX_SPREAD_PCT` (デフォルト: 1.0)
- **影響範囲**: `services/order-executor/handler.py`

#### 38. Chronos信頼度フィルター ✅ 実装済
- **問題**: confidence < 0.3 の低品質Chronos予測がそのままスコアに反映。ノイズシグナル
- **修正**: confidence < 0.3 ではスコアを `confidence / 0.3` 倍に減衰 (0.1→1/3に減衰)
- **影響範囲**: `services/aggregator/handler.py`

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
| 2026-02-10 | docs Phase 3 | `1647fe9` | ドキュメント更新 |
| 2026-02-10 | #21-23 Phase 4 | `5f0ba34` | daily-reporter + auto-improve + trades TTL |
| 2026-02-10 | #24-25 データ品質ゲート | `2b7022d` | daily-reporter品質チェック + auto-improve pre-check |
| 2026-02-10 | #26 F&G BUY抑制 | `bb5cfa2` | Extreme Fear時BUY閾値×1.35 |
| 2026-02-10 | #27 ゴミデータ削除 | - | trades 101件 + positions 39件を手動削除 |
| 2026-02-10 | #28 連続トレーリング | `ce37e58` | ピーク追跡 + 適応型トレール幅 |
| 2026-02-10 | #29 モメンタム減速 | `ce37e58` | MACD histogram slope + SELL閾値緩和 |
| 2026-02-11 | #30 Chronos SageMaker化 | `372722f` | Tiny(8M)→Base(200M), SageMaker Serverless |
| 2026-02-11 | #31 確信度ベース動的ウェイト | `372722f` | Chronos confidence→ウェイト±0.15動的変動 |
| 2026-02-12 | #32 TP/トレーリングストップ矛盾修正 | - | TP +10%→+30%安全弁, 判定順序入替 |
| 2026-02-12 | #33 負Kelly時スキップ | - | kelly_full≤0 → return 0 |
| 2026-02-12 | #34 DynamoDB Limit修正 | - | Limit=1→5, 3ファイル4箇所 |
| 2026-02-12 | #35 スコアクランプ | - | total_score [-1, 1] クランプ |
| 2026-02-12 | #36 bare except修正 | - | 例外特定化 + ログ追加 |
| 2026-02-12 | #37 スプレッドチェック | - | MAX_SPREAD_PCT=1.0% |
| 2026-02-12 | #38 Chronos信頼度フィルター | - | confidence<0.3 減衰 |
