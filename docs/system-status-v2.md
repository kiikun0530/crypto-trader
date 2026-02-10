# システム現状ステータス v3 — Phase 4.5 Data Quality + F&G BUY抑制後チェックポイント

**スナップショット日時**: 2026-02-11 JST  
**前回スナップショット**: v2 (`8b5f2a4`, Phase 3 Market Context + 閾値調整後)  
**最新コミット**: `bb5cfa2` (main) — F&G連動BUY閾値抑制  
**目的**: Phase 4 (Self-Improving Pipeline) + Phase 4.5 (Data Quality & F&G BUY抑制) の状態記録。

---

## 📌 次回調査ガイド（AIエージェント向け）

### やるべきこと

1. **F&G BUY閾値抑制の効果検証**
   - Extreme Fear (F&G≤20) 時にBUY_TH×1.35が正しく適用されているか確認
   - 02/10のETH/XRP損失のようなトレードがブロックされているか
   - ただしF&G回復時 (>20) にBUYが適切に発動するか

2. **自動改善パイプラインの動作検証**
   - daily-reporter (23:00 JST) が正常にレポート生成しているか
   - データ品質ゲート (`allow_improvement`) が正しく機能しているか
   - auto-improve.yml のpre-checkステップが低品質データをブロックしているか

3. **Phase 2-4 トレードの累積パフォーマンス**
   - VOL_CLAMP_MIN修正後 + Market Context導入後 + F&G BUY抑制後のトレード勝率
   - 口座残高推移 (¥130K → ¥~100K → 現在)

4. **DynamoDB データの健全性確認**
   - ゴミデータクリーンアップ (02/09 trades 101件 + positions 39件削除) 後の整合性
   - tradesテーブルのTTL (90日) が正しく動作しているか

5. **Chronos予測精度 vs F&G環境**
   - F&G≤20時のChronosスコア分布を調査
   - AI=+1.000のような異常スコアの発生頻度

### データ取得方法

```python
# DynamoDB trades テーブルからデータ取得
import boto3
dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1')
table = dynamodb.Table('eth-trading-trades')
response = table.scan()
trades = response['Items']

# Phase 2 デプロイ後のトレードを抽出 (Phase 2: 8211ac1 デプロイ = 2026-02-09 12:30頃 UTC)
PHASE2_DEPLOY_TS = 1739097000  # 2026-02-09 ~12:30 UTC
# VOL_CLAMP_MIN修正後のトレード (5ffcbea デプロイ = 2026-02-09 ~14:00 UTC)
VOLFIX_DEPLOY_TS = 1739102400  # 2026-02-09 ~14:00 UTC 概算

phase2_trades = [t for t in trades if float(t.get('timestamp', 0)) > PHASE2_DEPLOY_TS]
postfix_trades = [t for t in trades if float(t.get('timestamp', 0)) > VOLFIX_DEPLOY_TS]
```

```powershell
# AWS CLI でのスキャン
aws dynamodb scan --table-name eth-trading-trades --output json > data/trades_v2.json
aws dynamodb scan --table-name eth-trading-positions --output json > data/positions_v2.json
aws dynamodb scan --table-name eth-trading-signals --output json > data/signals_v2.json
```

### DynamoDB trades テーブルのスキーマ

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `order_id` | S | ✅ | パーティションキー |
| `pair` | S | ✅ | 通貨ペア (例: `xrp_jpy`) |
| `action` | S | ✅ | `buy` or `sell` |
| `rate` | N | ✅ | 約定レート (JPY) |
| `amount` | N | ✅ | 数量 |
| `timestamp` | N | ✅ | UNIX timestamp |
| `technical_score` | N | Phase 2+ | テクニカルスコア (-1.0 ~ +1.0) |
| `chronos_score` | N | Phase 2+ | Chronos予測スコア (-1.0 ~ +1.0) |
| `sentiment_score` | N | Phase 2+ | センチメントスコア (-1.0 ~ +1.0) |
| `weight_technical` | N | Phase 2+ | テクニカル重み (0.45) |
| `weight_chronos` | N | Phase 2+ | Chronos重み (0.25) |
| `weight_sentiment` | N | Phase 2+ | センチメント重み (0.15) |
| `weight_market_context` | N | Phase 3+ | マーケットコンテキスト重み (0.15) |
| `market_context_score` | N | Phase 3+ | マーケットコンテキストスコア |
| `buy_threshold` | N | Phase 2+ | 実効BUY閾値 (動的) |
| `sell_threshold` | N | Phase 2+ | 実効SELL閾値 (動的) |

> **Phase 3 トレードの見分け方**: `weight_technical=0.45` + `weight_market_context` が存在するレコードがPhase 3以降

### Slack ログの活用

- ユーザーが `tempdata` ファイルにSlackの取引通知ログを貼り付けて提供してくれる場合がある
- ログにはリアルタイムの score, threshold, regime, volume_multiplier 等が含まれる
- DynamoDBの `analysis_context` と突合すると完全な分析が可能

---

## 📊 パフォーマンス推移

### 期間別サマリ

| 期間 | 勝率 | 損益 | 取引数 | 備考 |
|------|------|------|--------|------|
| Phase 1 前 (バグ期) | 9.3% | ¥-30,000 | 43件 | entry_priceバグ含む |
| Phase 1 (10項目) | - | - | - | v1時点でデータ不足 |
| Phase 2 初日 | **0%** | **¥-775** | 3件 | VOL_CLAMP_MIN修正前 |
| Phase 2 + VOL_FIX | 未測定 | 未測定 | - | データ不足 |
| Phase 3 (MktCtx) | - | - | - | 02/10導入 |
| Phase 3 (02/10) | **0%** | **¥-3,162** | 2件 | ETH -¥1,661 + XRP -¥1,501 (F&G=14) |
| Phase 4.5 (F&G抑制後) | **未測定** | **未測定** | - | ← **次回ここを評価** |

### Phase 3 02/10 トレード詳細 (2件)

| # | 通貨 | アクション | スコア | BUY閾値 | Tech | AI | Sent | MktCtx | 損益 | 問題 |
|---|------|-----------|--------|---------|------|-----|------|--------|------|------|
| 1 | ETH | BUY→SELL | +高 | 0.366 | +0.510 | +1.000 | - | - | ¥-1,661 | **AI異常高スコア** (F&G=14) |
| 2 | XRP | BUY→SELL | +高 | 0.333 | +0.553 | +高 | - | - | ¥-1,501 | **Extreme Fear市場** |

> **分析結論**: Chronos AIがExtreme Fear (F&G=14) 環境で「反発」を予測 → 逆に下落。
> F&G≤20時にBUY_TH×1.35を適用していれば、これらのトレードはブロックされた。  
> → Phase 4.5で `bb5cfa2` にてF&G連動BUY閾値抑制を実装・デプロイ済み。

### 02/10 全トレード一覧 (tradesテーブル, ゴミ除去後)

| 時刻 | 通貨 | Action | Rate | BUY_TH | Tech |
|------|------|--------|------|--------|------|
| 02:03 | eth_jpy | BUY | ¥327,793 | 0.366 | +0.510 |
| 13:03 | eth_jpy | SELL | ¥321,612 | 0.348 | -0.572 |
| 13:13 | xrp_jpy | BUY | ¥225.4 | 0.333 | +0.553 |
| 15:53 | xrp_jpy | SELL | ¥220.2 | 0.350 | -0.389 |
| 20:43 | sol_jpy | BUY | ¥13,360.5 | 0.188 | +0.504 |

---

## 🔧 現在の設定値一覧

### Aggregator (`services/aggregator/handler.py`, ~710行)

| パラメータ | 環境変数 | 現在値 | Phase 1 | Phase 2変更 | 変更理由 |
|-----------|---------|--------|---------|------------|---------|
| テクニカル重み | `TECHNICAL_WEIGHT` | **0.45** | 0.45 | `72cf12f` | #20 4成分化 |
| Chronos重み | `AI_PREDICTION_WEIGHT` | **0.25** | 0.40 | `72cf12f` | #20 4成分化 |
| センチメント重み | `SENTIMENT_WEIGHT` | 0.15 | 0.15 | - | 変更なし |
| マーケットCtx重み | `MARKET_CONTEXT_WEIGHT` | **0.15** | - | `72cf12f` | #20 新規 |
| BUY基準閾値 | `BASE_BUY_THRESHOLD` | **0.28** | 0.20 | `8b5f2a4` | #20a 4成分圧縮補正 |
| SELL基準閾値 | `BASE_SELL_THRESHOLD` | **-0.15** | -0.20 | `8b5f2a4` | #20a 4成分圧縮補正 |
| BB幅基準 | `BASELINE_BB_WIDTH` | 0.03 | 0.03 | - | 変更なし |
| ボラ補正下限 | `VOL_CLAMP_MIN` | **0.67** | 0.50 | `5ffcbea` | #19 最低BUY閾値0.15→0.20 |
| ボラ補正上限 | `VOL_CLAMP_MAX` | 2.0 | 2.0 | - | 変更なし |
| 最低保有時間 | `MIN_HOLD_SECONDS` | **1800** | なし | - | #2 (Phase 1) |
| 通貨最大同時保有 | `MAX_POSITIONS_PER_PAIR` | **1** | 制限なし | - | #6 (Phase 1) |
| F&G恐怖閾値 | - | **20** | - | `bb5cfa2` | #26 F&G BUY抑制 |
| F&G強欲閾値 | - | **80** | - | `bb5cfa2` | #26 F&G BUY抑制 |
| F&G Fear BUY倍率 | - | **×1.35** | - | `bb5cfa2` | #26 BUY閾値35%引上げ |
| F&G Greed BUY倍率 | - | **×1.20** | - | `bb5cfa2` | #26 BUY閾値20%引上げ |

#### 動的BUY閾値の計算式
```
vol_ratio = avg_bb_width / BASELINE_BB_WIDTH(0.03)
vol_ratio = clamp(vol_ratio, VOL_CLAMP_MIN(0.67), VOL_CLAMP_MAX(2.0))
BUY_threshold = BASE_BUY_THRESHOLD(0.28) × vol_ratio
SELL_threshold = BASE_SELL_THRESHOLD(-0.15) × vol_ratio

# F&G連動補正 (Phase 4.5)
if F&G ≤ 20: BUY_threshold × 1.35
if F&G ≥ 80: BUY_threshold × 1.20
# SELL_thresholdはF&G補正なし

→ 通常BUY範囲: [0.19, 0.56]
→ Fear時BUY範囲: [0.25, 0.76]  (×1.35)
→ Greed時BUY範囲: [0.23, 0.67] (×1.20)
→ SELL範囲: [-0.10, -0.30]     (変更なし)
```

### Technical (`services/technical/handler.py`, ~488行)

| 機能 | 現在の実装 | 改善前 | 変更コミット |
|------|-----------|--------|------------|
| MACDシグナルライン | **EMA(9) of MACD series** | `macd * 0.9` (偽) | `95ef463` |
| MACDスコアリング | **ヒストグラム振幅グラデーション** (price%正規化) | バイナリ | `8211ac1` |
| BBスコアリング | **線形グラデーション** + バンド外1.2xボーナス | ステップ関数 | `5c12caa` |
| RSI計算方式 | **Wilder's 指数平滑** (SMA init + EMA) | 単純平均 | `8211ac1` |
| ATR/ADX計算 | **正式OHLC True Range** | close近似 | `8211ac1` |
| Volumeシグナル | **出来高乗数 1.0-1.3** (20本平均比、1.5xで開始) | なし | `8211ac1` |
| レジーム検知 | **ADX判定** (>25:トレンド, <20:レンジ) | なし | `5c12caa` |
| レジーム別ウェイト | トレンド: MACD/SMA=0.35, RSI/BB=0.15 | 均等 0.25 | `5c12caa` |

### Chronos Caller (`services/chronos-caller/handler.py`, ~433行)

| パラメータ | 現在値 | 改善前 | 変更理由 |
|-----------|--------|--------|---------|
| スコア変換スケール | **±1% = ±1.0** | ±5% = ±1.0 | #4 機能化 |
| 入力データ | **Typical Price (H+L+C)/3** | closeのみ | ローソク足重心 |
| デコード | **KVキャッシュ付き** | フルデコード | #16 高速化 |
| 予測ステップ | 12 | 12 | - |
| サンプル数 | 20 | 20 | - |

### Position Monitor (`services/position-monitor/handler.py`)

| 機能 | 現在値 | 備考 |
|------|--------|------|
| SL | -5% (固定初期値) | |
| TP | +10% (固定初期値) | |
| トレーリングストップ | +3%→SL=建値, +5%→SL=+3%, +8%→SL=+6% | |
| ポーリング間隔 | **5分** | EventBridge。急落キャッチに限界あり |

### Order Executor (`services/order-executor/handler.py`, ~1027行)

| 機能 | 環境変数 | 現在値 | 備考 |
|------|---------|--------|------|
| サーキットブレーカー | `CIRCUIT_BREAKER_ENABLED` | **false** (OFF) | #10 |
| 日次損失上限 | `CB_DAILY_LOSS_LIMIT_JPY` | 15,000 | |
| 連敗上限 | `CB_MAX_CONSECUTIVE_LOSSES` | 5 | |
| 冷却時間 | `CB_COOLDOWN_HOURS` | 6 | |
| 最大ポジション額 | `MAX_POSITION_JPY` | 15,000 | |
| 予備資金 | `RESERVE_JPY` | 1,000 | |
| 最小注文額 | `MIN_ORDER_JPY` | 500 | |

---

## ✅ 全改善実装履歴 (#1-#19)

### Phase 1 (10項目, `a986c13`)

| # | 改善内容 | コミット | Lambda |
|---|---------|---------|--------|
| 1 | MACDシグナルライン EMA(9) 修正 | `95ef463` | technical |
| 2 | 最低保有時間 30分 | `8e582d0` | aggregator |
| 3 | BB線形グラデーション化 | `5c12caa` | technical |
| 4 | Chronosスコアスケール ±5%→±1% | `95ef463` | chronos-caller |
| 5 | トレーリングストップ 3段階 | `5c12caa` | position-monitor |
| 6 | 通貨分散 MAX_POSITIONS_PER_PAIR=1 | `5c12caa` | aggregator |
| 7 | ADXレジーム検知 + 適応型ウェイト | `5c12caa` | technical |
| 8 | テクニカル指標拡充 (ADX/ATR/ATR%) | `5c12caa` | technical |
| 9 | BUY閾値 0.20→0.30 | `5c12caa` | aggregator |
| 10 | サーキットブレーカー (デフォルトOFF) | `a986c13` | order-executor |

### Phase 2 (8+1項目, `8211ac1` + `90684dc`)

| # | 改善内容 | コミット | Lambda |
|---|---------|---------|--------|
| 11 | OHLCVデータ保存・ATR/ADX正式化 | `8211ac1` | price-collector, technical |
| 12 | MACDヒストグラムグラデーション | `8211ac1` | technical |
| 13 | RSI Wilder's指数平滑化 | `8211ac1` | technical |
| 14 | Volumeシグナル (1.0-1.3x乗数) | `8211ac1` | technical |
| 15 | ウェイトデータドリブン (0.55/0.30/0.15) | `8211ac1` | aggregator |
| 16 | KVキャッシュデコード | `8211ac1` | chronos-caller |
| 17 | NLPセンチメント高度化 | `8211ac1` | news-collector |
| 18 | トレードコンテキスト保存 | `8211ac1` | aggregator, order-executor |
| - | Chronos Typical Price (H+L+C)/3 | `90684dc` | chronos-caller |

### Phase 3 (運用データフィードバック + Market Context)

| # | 改善内容 | コミット | Lambda |
|---|---------|---------|--------|
| 19 | VOL_CLAMP_MIN 0.5→0.67 | `5ffcbea` | aggregator |
| 17a | NLP "buy the dip" コンテキスト修正 | `aa138cf` | news-collector |
| 20 | Market Context 第4の柱 (F&G/Funding/BTC Dom) | `72cf12f` | market-context(新規), aggregator |
| 20a | 4成分化閾値調整 (BUY 0.28, SELL -0.15) | `8b5f2a4` | aggregator |

### Phase 4 (Self-Improving Pipeline)

| # | 改善内容 | コミット | Lambda/CI |
|---|---------|---------|-----------|
| 21 | Daily Reporter Lambda | `5f0ba34` | daily-reporter(新規) |
| 22 | Auto-Improve Pipeline | `5f0ba34` | auto-improve.yml(新規) |
| 23 | Trades テーブル TTL 90日 | `5f0ba34` | order-executor |

### Phase 4.5 (Data Quality & Noise Protection)

| # | 改善内容 | コミット | Lambda/CI |
|---|---------|---------|-----------|
| 24 | データ品質ゲート (Wilson CI + min trades) | `2b7022d` | daily-reporter |
| 25 | Auto-Improve Pre-Check ゲート | `2b7022d` | auto-improve.yml |
| 26 | F&G連動BUY閾値抑制 | `bb5cfa2` | aggregator |
| 27 | ゴミデータクリーンアップ | - | trades 101件 + positions 39件手動削除 |

---

## 🏗️ システム構成

### Lambda関数

| 関数名 | 主要機能 | 最終デプロイ | 行数 |
|--------|---------|------------|------|
| `eth-trading-price-collector` | 6通貨価格収集 (Binance OHLCV) | 2026-02-09 | ~180 |
| `eth-trading-technical` | RSI/MACD/SMA/BB/ADX/ATR/Volume | 2026-02-09 | ~488 |
| `eth-trading-chronos-caller` | ONNX Chronos-T5-Tiny (KVキャッシュ+Typical Price) | 2026-02-09 | ~433 |
| `eth-trading-sentiment-getter` | CryptoPanic センチメント | - | - |
| `eth-trading-news-collector` | ニュース収集 + BTC相関 + NLPセンチメント | 2026-02-09 | ~370+ |
| `eth-trading-aggregator` | 4成分統合スコアリング + 売買判定 + F&G BUY抑制 | 2026-02-10 | ~710 |
| `eth-trading-order-executor` | Coincheck 成行注文 + CB + context保存 | 2026-02-09 | ~1027 |
| `eth-trading-position-monitor` | SL/TP/トレーリング (5分間隔) | 2026-02-09 | - |
| `eth-trading-market-context` | F&G/Funding/BTC Dom収集 (30分間隔) | 2026-02-10 | ~300 |
| `eth-trading-error-remediator` | エラー検知→Slack→自動修復 | - | - |
| `eth-trading-daily-reporter` | 日次レポート→自動改善トリガー + データ品質ゲート (23:00 JST) | 2026-02-10 | ~794 |

### AWS環境

| 項目 | 値 |
|------|-----|
| AWSアカウント | 652679684315 |
| リージョン | ap-northeast-1 |
| GitHub | kiikun0530/crypto-trader |
| Lambda実行環境 | Python 3.11 |
| ローカルPython | 3.12 venv (`C:/Users/kiiku/crypto-trader/.venv/Scripts/python.exe`) |
| boto3 | venvにインストール済み |

### デプロイ手順

```powershell
# Lambda デプロイ (例: aggregator)
Compress-Archive -Path "services/aggregator/handler.py" -DestinationPath "aggregator.zip" -Force
aws lambda update-function-code --function-name eth-trading-aggregator --zip-file fileb://aggregator.zip --region ap-northeast-1

# 複数ファイルを含む場合 (例: chronos-caller + onnx_model)
Compress-Archive -Path "services/chronos-caller/*" -DestinationPath "chronos.zip" -Force
```

### データフロー

```
[5分間隔] EventBridge → price-collector → DynamoDB(prices)
                                 ↓ (変動≥0.3% or 1h経過)
                          Step Functions
                              ↓ Map (×6通貨: ETH,BTC,XRP,SOL,DOGE,AVAX)
                    ┌─────────┼─────────┐
                technical  chronos  sentiment
                    └─────────┼─────────┘
                          aggregator
                              │ + DynamoDB(market-context) ← market-context Lambda (30分毎)
                              ↓ 4成分加重 (0.45/0.25/0.15/0.15)
                              ↓ (BUY: score>0.28×vol / SELL: score<-0.15×vol)
                          SQS → order-executor → Coincheck API
                                                      ↓
[5分間隔] EventBridge → position-monitor → SL/TP/トレーリング判定
                              ↓ (トリガー時)
                          SQS → order-executor → Coincheck API

[30分間隔] EventBridge → market-context → DynamoDB(market-context)
                              ↑ Alternative.me (F&G)
                              ↑ Binance Futures (Funding)
                              ↑ CoinGecko (BTC Dom)
```

---

## 🐛 過去の重要バグと修正

| バグ | 影響 | 修正コミット | 発見方法 |
|------|------|------------|---------|
| entry_price膨張 | 43件中~26件でP/L計算不能 | `45642e7` | DynamoDB分析 |
| MACD偽シグナル (`macd*0.9`) | 全件でMACD判定が無意味 | `95ef463` | コード監査 |
| `analysis_context` 未定義 | order-executor Lambda Error | `4705a49` | CloudWatch Logs |
| auto-fix workflow timeout | 自動修復が途中で切れる | `4705a49` | GitHub Actions |
| VOL_CLAMP_MIN低すぎ | 限界的シグナルが通過 (BTC/AVAX) | `5ffcbea` | Phase 2初日分析 |
| order_idなしfill取得 | 101ゴミtrades + 39ゴミpositions | 修正済(既存) | DynamoDB分析 |
| Extreme Fear時AI高スコア | ETH/XRP ¥-3,162損失 | `bb5cfa2` | 02/10トレード分析 |

---

## 🔍 既知の制約・今後の検討事項

### アーキテクチャ制約
- **ポーリング間隔5分**: position-monitorは5分周期。急落時にSL/TPが遅延する (AVAX: ¥1,499→¥1,411の急落をキャッチできず)
- **対策案**: EventBridge間隔短縮 (1分), WebSocket Lambda, or SNS price alert → コスト増

### サーキットブレーカー
- 現在OFF。十分なデータ蓄積後にON判断
- ONにする場合: Lambda環境変数 `CIRCUIT_BREAKER_ENABLED=true`

### 将来の改善候補

| アイデア | 優先度 | 前提条件 |
|----------|--------|---------|
| ~~Market Context第4の柱~~ | ~~中~~ | ✅ Phase 3で実装済 |
| ~~自動改善パイプライン~~ | ~~高~~ | ✅ Phase 4で実装済 |
| ~~データ品質ゲート~~ | ~~高~~ | ✅ Phase 4.5で実装済 |
| ~~F&G BUY閾値抑制~~ | ~~高~~ | ✅ Phase 4.5で実装済 |
| VOL_CLAMP_MIN 微調整 (0.67→?) | 中 | 修正後データ蓄積 |
| サーキットブレーカー ON | 中 | 連敗パターン分析 |
| トレーリングストップ段階追加 | 中 | 利確パターン分析 |
| ポーリング間隔短縮 (5分→1分) | 中 | コスト試算 |
| 時間帯別ウェイト調整 | 低 | 時間帯別勝率データ |
| Chronosモデルサイズ拡大 | 低 | 精度実測データ |
| ML/統計モデルによる閾値最適化 | 低 | 100件+のトレードデータ |

---

## 📝 コミット履歴（全件）

```
bb5cfa2 feat: F&G-linked BUY threshold suppression (Fear×1.35, Greed×1.20)
2b7022d feat: data quality gate + auto-improve pre-check (Phase 4.5)
57fa17b fix: daily-reporter deployment fix
5f0ba34 feat: add self-improving pipeline (daily-reporter + auto-improve) Phase 4
1647fe9 docs: update Phase 3 documentation
8b5f2a4 feat: adjust thresholds for 4-component scoring (BUY 0.30→0.28, SELL -0.20→-0.15)
72cf12f feat: add market-context 4th pillar (F&G + Funding + BTC Dom) (#20)
aa138cf fix: NLP buy-the-dip context recognition
5ffcbea feat: raise VOL_CLAMP_MIN 0.5→0.67 (#19) - min BUY threshold 0.15→0.20
4705a49 fix: order-executor analysis_context NameError + auto-fix workflow timeout
37bfe9b chore: hashtag change #暗号通貨自動売買 → #自動売買
3bd7421 docs: update trading strategy and system status for Phase 2
90684dc feat: Chronos Typical Price (H+L+C)/3
8211ac1 feat: Phase 2 improvements #11-#18 (MACD gradient, RSI Wilder's, OHLCV, Volume, weights, KV cache, NLP, trade context)
a986c13 feat: implement circuit breaker (#10) - default OFF
5c12caa feat: implement trading improvements #3,5,6,7,8,9
95ef463 feat: fix MACD signal line (EMA9) + Chronos score scale 5%→1%
8e582d0 feat: minimum hold period (30min)
f6c29db fix(news-collector): panic_score scale fix
45642e7 fix(critical): entry_price sanity check
```

---

## 🏦 口座残高推移

| 時点 | 残高 | 変動 | 備考 |
|------|------|------|------|
| 開始 | ¥130,000 | - | Coincheck入金額 |
| Phase 1 バグ期後 | ¥~100,000 | ¥-30,000 | entry_priceバグ + 即売り |
| Phase 2 初日後 | ¥~98,000 (推定) | ¥-775 (Phase 2分) + α | VOL_CLAMP_MIN修正前の3トレード + SOL/DOGE |
| 次回確認 | **未測定** | - | ← **Coincheck残高を確認すること** |

> ⚠️ DynamoDBのentry_price/amountはPhase 1バグで膨張しており、per-tradeのP/L計算は信頼できない。
> 実際のP/LはCoincheck口座残高の変動で確認すること。
> Phase 2以降は `analysis_context` があるため、`rate` フィールドとBUY/SELL突合で正確なP/L算出が可能。
