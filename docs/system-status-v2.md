# システム現状ステータス v2 — Phase 3 Market Context + 閾値調整後チェックポイント

**スナップショット日時**: 2026-02-10 JST  
**前回スナップショット**: v1 (`a986c13`, Phase 1 完了時点)  
**最新コミット**: `8b5f2a4` (main) — 4成分化閾値調整 (BUY 0.28, SELL -0.15)  
**目的**: Phase 2 実装 + Phase 3 Market Context第4の柱 + 閾値調整後の状態記録。次回調査の起点。

---

## 📌 次回調査ガイド（AIエージェント向け）

### やるべきこと

1. **VOL_CLAMP_MIN=0.67 の効果検証**
   - BUY閾値の最低値が 0.15→0.20 に上がった
   - スコア0.20未満の限界的シグナルが正しくブロックされているか確認
   - 逆に有効なシグナルまでブロックされていないかチェック

2. **Market Context 第4の柱の効果検証** (新規)
   - 4成分体制 (Tech=0.45, Chronos=0.25, Sent=0.15, MktCtx=0.15) のバランス確認
   - Fear & Greed / Funding Rate / BTC Dominance がシグナル品質に与える影響
   - 調整後閾値 (BUY=0.28, SELL=-0.15) でのシグナル頻度が適切か

3. **Phase 2/3 トレードの勝率改善確認**
   - Phase 2 初日は 0勝3敗 (XRP ¥-462, BTC ¥-102, AVAX ¥-211 = 合計 ¥-775)
   - VOL_CLAMP_MIN修正後のトレードで改善しているか

3. **DynamoDB `analysis_context` の活用**
   - #18で保存されるようになった `technical_score`, `chronos_score`, `sentiment_score`, `weight_*`, `buy_threshold`, `sell_threshold` を活用
   - 各トレードの発火理由を分析し、パターンを特定

4. **Chronos予測精度の評価**
   - Chronos重み0.30 → 寄与度を実績トレードで検証
   - near-zero率が依然52%ならさらなるウェイト削減を検討

5. **口座残高の推移確認**
   - ¥130K → ¥100K (Phase 1損失) → Phase 2以後の変動を確認

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
| Phase 2 + VOL_FIX | **未測定** | **未測定** | - | ← **次回ここを評価** |

### Phase 2 初日トレード詳細 (3件)

| # | 通貨 | アクション | スコア | BUY閾値 | Tech | Chronos | Sent | 損益 | 問題 |
|---|------|-----------|--------|---------|------|---------|------|------|------|
| 1 | XRP | BUY→SELL | 0.222 | 0.15 | +0.37 | +0.04 | +0.04 | ¥-462 | 正当シグナル、市場環境が悪い |
| 2 | BTC | BUY→SL | 0.159 | 0.15 | +0.26 | -0.01 | +0.02 | ¥-102 | **限界的シグナル** (0.009超過のみ) |
| 3 | AVAX | BUY→SL | 0.165 | 0.15 | +0.43 | -0.11 | -0.08 | ¥-211 | **限界的シグナル** (Tech単独、AI/Sent反対) |

> **分析結論**: BTC/AVAXはVOL_CLAMP_MIN=0.67なら閾値0.20未満でブロックされた（計¥313の損失回避）。
> XRPはスコア0.222 > 0.20で依然発動する正当なシグナル。市場環境の問題。

### AVAX トレーリングストップの動作記録

- 含み益 +5.37% (¥1,499) まで到達 → SL=建値+3%に引上げ (正常動作)
- しかし5分後のポーリングで ¥1,499→¥1,411 に急落 → SL ¥1,399で執行
- **課題**: 5分間隔のposition-monitorでは急落をキャッチできない（アーキテクチャ制約）
- 対策案: ポーリング間隔短縮 or WebSocket監視 (コスト増大のため要検討)

---

## 🔧 現在の設定値一覧

### Aggregator (`services/aggregator/handler.py`, ~580行)

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

#### 動的BUY閾値の計算式
```
vol_ratio = avg_bb_width / BASELINE_BB_WIDTH(0.03)
vol_ratio = clamp(vol_ratio, VOL_CLAMP_MIN(0.67), VOL_CLAMP_MAX(2.0))
BUY_threshold = BASE_BUY_THRESHOLD(0.28) × vol_ratio
SELL_threshold = BASE_SELL_THRESHOLD(-0.15) × vol_ratio
→ BUY範囲: [0.19, 0.56]
→ SELL範囲: [-0.10, -0.30]
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
| `eth-trading-aggregator` | 4成分統合スコアリング + 売買判定 + context | 2026-02-10 | ~660 |
| `eth-trading-order-executor` | Coincheck 成行注文 + CB + context保存 | 2026-02-09 | ~1027 |
| `eth-trading-position-monitor` | SL/TP/トレーリング (5分間隔) | 2026-02-09 | - |
| `eth-trading-market-context` | F&G/Funding/BTC Dom収集 (30分間隔) | 2026-02-10 | ~300 |
| `eth-trading-error-remediator` | エラー検知→Slack→自動修復 | - | - |

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
| Market Context第4の柱 | 中 | Phase 3効果検証 |
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
