"""
アグリゲーター Lambda
全通貨のテクニカル、Chronos、センチメントスコアを統合
通貨毎にBUY/SELL/HOLDを判定して記録・送信

マルチ通貨ロジック:
- 全通貨をスコアリングしてランキング
- 通貨毎にポジション非依存でBUY/SELL/HOLD判定
- 判定結果をDynamoDB(signals)に記録
- BUY/SELLがある場合のみSQSにバッチ送信（1メッセージ）
- ボラティリティ適応型閾値（市場状況に応じて動的調整）
- order-executorが残高・ポジション確認して実際の注文を実行
"""
import json
import os
import time
import traceback
import boto3
from decimal import Decimal, ROUND_HALF_UP
import urllib.request
from trading_common import (
    TRADING_PAIRS, POSITIONS_TABLE, SLACK_WEBHOOK_URL,
    get_current_price, get_active_position, send_slack_notification, dynamodb
)

sqs = boto3.client('sqs')
bedrock = boto3.client('bedrock-runtime')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'apac.amazon.nova-micro-v1:0')

SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
MARKET_CONTEXT_TABLE = os.environ.get('MARKET_CONTEXT_TABLE', 'eth-trading-market-context')
ORDER_QUEUE_URL = os.environ.get('ORDER_QUEUE_URL', '')

# 重み設定 (4コンポーネント: Tech + Chronos + Sentiment + MarketContext)
# Phase 2: Tech dominant (0.55) → Phase 3: 4成分分散 → Phase 4: AI重視均等化
# Phase 4: AI(Chronos)の予測精度向上に伴い、TechとAIを同等の基準重みに変更
# MarketContext = Fear&Greed + FundingRate + BTC Dominance (市場マクロ環境)
TECHNICAL_WEIGHT = float(os.environ.get('TECHNICAL_WEIGHT', '0.35'))
CHRONOS_WEIGHT = float(os.environ.get('AI_PREDICTION_WEIGHT', '0.35'))
SENTIMENT_WEIGHT = float(os.environ.get('SENTIMENT_WEIGHT', '0.15'))
MARKET_CONTEXT_WEIGHT = float(os.environ.get('MARKET_CONTEXT_WEIGHT', '0.15'))

# ボラティリティ適応型閾値
# 基準閾値（平均的なボラティリティ時に使用）
# Phase 4: Tech重み削減(0.45→0.35)でスコア圧縮 + AI均等化
# 旧 BUY=0.28 / SELL=-0.15 → 新 BUY=0.25 / SELL=-0.13
BASE_BUY_THRESHOLD = float(os.environ.get('BASE_BUY_THRESHOLD', '0.25'))
BASE_SELL_THRESHOLD = float(os.environ.get('BASE_SELL_THRESHOLD', '-0.13'))
# BB幅の基準値（暗号通貨の典型的なBB幅 ≈ 3%）
BASELINE_BB_WIDTH = float(os.environ.get('BASELINE_BB_WIDTH', '0.03'))
# ボラティリティ補正のクランプ範囲
# MIN=0.67: 最低BUY閾値 0.30×0.67=0.20（限界的シグナルでの誤エントリー防止）
VOL_CLAMP_MIN = 0.67
VOL_CLAMP_MAX = 2.0

# 最低保有時間（秒）: 表示用（実際の制御はorder-executorで実施）
MIN_HOLD_SECONDS = int(os.environ.get('MIN_HOLD_SECONDS', '1800'))  # デフォルト30分


def handler(event, context):
    """全通貨の統合スコア計算 + 最適通貨選定"""

    # Step Functionsから Map → analysis_results 形式で受け取る
    pairs_results = event.get('analysis_results', [])

    # 後方互換: 単一ペアの旧形式
    if not pairs_results and 'pair' in event:
        pairs_results = [event]

    try:
        # 0. マーケットコンテキスト取得（全通貨共通のマクロ情報）
        market_context = fetch_market_context()

        # 1. 全通貨のスコア計算
        scored_pairs = []
        for result in pairs_results:
            pair = result.get('pair', 'unknown')
            scored = score_pair(pair, result, market_context)
            scored_pairs.append(scored)

        # 2. 通貨別ボラティリティ適応型閾値を計算（F&G連動補正付き）
        thresholds_map = calculate_per_currency_thresholds(scored_pairs, market_context)

        # 3. AI総合コメント生成 + シグナル保存（通貨別閾値を使用）
        for scored in scored_pairs:
            pair_th = thresholds_map.get(scored['pair'], {'buy': BASE_BUY_THRESHOLD, 'sell': BASE_SELL_THRESHOLD})
            ai_comment = generate_ai_comment(scored, pair_th)
            scored['ai_comment'] = ai_comment
            save_signal(scored, pair_th['buy'], pair_th['sell'])

        # 4. スコア順にソート（期待値の高い順）
        scored_pairs.sort(key=lambda x: x['total_score'], reverse=True)

        # 5. 通貨毎のBUY/SELL/HOLD判定（通貨別閾値・ポジション非依存）
        per_currency_decisions = decide_per_currency_signals(
            scored_pairs, thresholds_map
        )

        # 6. 非HOLDの判定を抽出
        actionable_decisions = [d for d in per_currency_decisions if d['signal'] != 'HOLD']
        has_signal = len(actionable_decisions) > 0

        # 7. キューにバッチ送信（BUY/SELLがある場合のみ）
        if has_signal and ORDER_QUEUE_URL:
            send_batch_order_message(
                actionable_decisions, int(time.time())
            )

        # 8. ポジション取得（表示用）
        active_positions = find_all_active_positions()

        # 通貨別判定の集計
        buy_decisions = [d for d in per_currency_decisions if d['signal'] == 'BUY']
        sell_decisions = [d for d in per_currency_decisions if d['signal'] == 'SELL']
        hold_decisions = [d for d in per_currency_decisions if d['signal'] == 'HOLD']

        result = {
            'decisions': [
                {
                    'pair': d['analysis_pair'],
                    'coincheck_pair': d['pair'],
                    'signal': d['signal'],
                    'score': round(d['score'], 4)
                }
                for d in per_currency_decisions
            ],
            'summary': {
                'buy': len(buy_decisions),
                'sell': len(sell_decisions),
                'hold': len(hold_decisions),
            },
            'has_signal': has_signal,
            'ranking': [
                {
                    'pair': s['pair'],
                    'name': TRADING_PAIRS.get(s['pair'], {}).get('name', s['pair']),
                    'score': round(s['total_score'], 4)
                }
                for s in scored_pairs
            ],
            'active_positions': [p.get('pair') for p in active_positions],
            'thresholds': {pair: {'buy': th['buy'], 'sell': th['sell']} for pair, th in thresholds_map.items()},
            'timestamp': int(time.time())
        }

        # 9. Slack通知（ランキング付き + 通貨別判定 + 含み損益表示）
        notify_slack(result, scored_pairs, active_positions,
                     thresholds_map, per_currency_decisions)

        return result

    except Exception as e:
        print(f"Error in handler: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'signal': 'HOLD',
            'has_signal': False,
            'error': str(e)
        }


def score_pair(pair: str, result: dict, market_context: dict = None) -> dict:
    """通貨ペアのスコアを計算（4コンポーネント + 確信度ベース動的重み）"""
    technical_result = result.get('technical', {})
    chronos_result = result.get('chronos', {})
    sentiment_result = result.get('sentiment', {})

    technical_score = extract_score(technical_result, 'technical_score', 0.5)
    chronos_score = extract_score(chronos_result, 'chronos_score', 0.5)
    sentiment_score = extract_score(sentiment_result, 'sentiment_score', 0.5)

    # Chronos確信度を取得 (SageMaker版で追加)
    chronos_confidence = 0.5  # デフォルト
    if isinstance(chronos_result, dict):
        if 'body' in chronos_result:
            body = json.loads(chronos_result['body']) if isinstance(chronos_result['body'], str) else chronos_result['body']
            chronos_confidence = float(body.get('confidence', 0.5))
        else:
            chronos_confidence = float(chronos_result.get('confidence', 0.5))

    # -1〜1スケールに正規化
    technical_normalized = technical_score  # 既に-1〜1
    chronos_normalized = chronos_score  # 既に-1〜1
    sentiment_normalized = (sentiment_score - 0.5) * 2  # 0〜1 → -1〜1

    # Chronos信頼度フィルター: 低確信度の予測を減衰
    # confidence < 0.3 → スコアを confidence/0.3 倍に減衰（ノイズ予測の影響を抑制）
    # confidence >= 0.3 → そのまま
    CHRONOS_MIN_CONFIDENCE = 0.3
    if chronos_confidence < CHRONOS_MIN_CONFIDENCE:
        damping = chronos_confidence / CHRONOS_MIN_CONFIDENCE
        original = chronos_normalized
        chronos_normalized *= damping
        print(f"  Chronos confidence filter: {chronos_confidence:.3f} < {CHRONOS_MIN_CONFIDENCE} "
              f"→ score damped {original:.3f} → {chronos_normalized:.3f}")

    # マーケットコンテキストスコア（DynamoDB直接読み取り）
    market_context_normalized = 0.0  # デフォルト中立
    market_context_detail = {}
    if market_context:
        market_context_normalized = float(market_context.get('market_score', 0))
        market_context_detail = {
            'fng_value': market_context.get('fng_value', 50),
            'fng_classification': market_context.get('fng_classification', 'N/A'),
            'fng_score': float(market_context.get('fng_score', 0)),
            'funding_score': float(market_context.get('funding_score', 0)),
            'dominance_score': float(market_context.get('dominance_score', 0)),
            'btc_dominance': float(market_context.get('btc_dominance', 50)),
            'avg_funding_rate': float(market_context.get('avg_funding_rate', 0)),
        }

    # BTC Dominanceによるアルトコイン追加補正
    # BTC自体はDominance上昇で有利、アルト（ETH, XRP, SOL, DOGE, AVAX）は不利
    alt_dominance_adjustment = 0.0
    if market_context and pair != 'btc_usdt':
        btc_dom = float(market_context.get('btc_dominance', 50))
        # BTC Dominance 60%超 → アルトに追加ペナルティ (-0.05)
        # BTC Dominance 40%未満 → アルトにボーナス (+0.05)
        if btc_dom > 60:
            alt_dominance_adjustment = -0.05
        elif btc_dom < 40:
            alt_dominance_adjustment = 0.05

    # === 確信度ベース動的重み ===
    # Phase 4: TechとAIが同等基準重み(0.35)のため、シフト幅を±0.08に縮小
    # 高確信度 → Chronos重み増加 (最大0.43), Tech重み減少 (最小0.27)
    # 低確信度 → Chronos重み減少 (最小0.27), Tech重み増加 (最大0.43)
    # 中間 (0.5) → ベース値通り (0.35/0.35)
    base_chronos_w = CHRONOS_WEIGHT  # 0.35
    base_tech_w = TECHNICAL_WEIGHT   # 0.35

    # confidence: 0.0~1.0 → weight_shift: -0.08 ~ +0.08
    # confidence=0.0 → shift=-0.08 (Chronos: 0.27, Tech: 0.43)
    # confidence=1.0 → shift=+0.08 (Chronos: 0.43, Tech: 0.27)
    weight_shift = (chronos_confidence - 0.5) * 0.16  # ±0.08 range, centered at 0.5
    weight_shift = max(-0.08, min(0.08, weight_shift))

    effective_chronos_w = base_chronos_w + weight_shift
    effective_tech_w = base_tech_w - weight_shift  # Techで相殺

    # 4成分加重平均 (確信度ベース動的重み)
    total_score = (
        technical_normalized * effective_tech_w +
        chronos_normalized * effective_chronos_w +
        sentiment_normalized * SENTIMENT_WEIGHT +
        market_context_normalized * MARKET_CONTEXT_WEIGHT +
        alt_dominance_adjustment
    )

    # スコアを[-1, 1]にクランプ（alt_dominance_adjustmentで範囲を超えうるため）
    total_score = max(-1.0, min(1.0, total_score))

    # ボラティリティ情報を抽出（BB幅 = (上限-下限)/中央値）
    bb_width = extract_bb_width(technical_result)

    # モメンタム変化率を抽出（MACDヒストグラムの傾き）
    macd_histogram_slope = extract_indicator(technical_result, 'macd_histogram_slope', 0.0)
    macd_histogram = extract_indicator(technical_result, 'macd_histogram', 0.0)

    # === 根拠データ抽出（シグナル解説用） ===
    # テクニカル指標の生データ
    indicators_detail = _extract_raw_indicators(technical_result)

    # Chronos予測の詳細
    chronos_detail = _extract_chronos_detail(chronos_result)

    # ニュースヘッドライン（sentiment-getterがtop_headlinesを含む）
    news_headlines = _extract_news_headlines(sentiment_result)

    return {
        'pair': pair,
        'total_score': total_score,
        'components': {
            'technical': round(technical_normalized, 3),
            'chronos': round(chronos_normalized, 3),
            'sentiment': round(sentiment_normalized, 3),
            'market_context': round(market_context_normalized, 3)
        },
        'weights': {
            'technical': round(effective_tech_w, 3),
            'chronos': round(effective_chronos_w, 3),
            'sentiment': SENTIMENT_WEIGHT,
            'market_context': MARKET_CONTEXT_WEIGHT,
        },
        'chronos_confidence': round(chronos_confidence, 3),
        'market_context_detail': market_context_detail,
        'macd_histogram_slope': round(macd_histogram_slope, 4),
        'macd_histogram': round(macd_histogram, 4),
        # ⚠️ この価格はBinance USDT建て（例: ETH ~$2,100）
        # Coincheck JPY建てのポジション価格と比較してはいけない
        # P/L計算にはget_current_price()でJPY価格を別途取得すること
        'current_price_usd': result.get('technical', {}).get('current_price', 0),
        'bb_width': bb_width,
        'indicators_detail': indicators_detail,
        'chronos_detail': chronos_detail,
        'news_headlines': news_headlines,
    }


def extract_bb_width(technical_result: dict) -> float:
    """テクニカル結果からBB幅（ボラティリティ指標）を抽出"""
    try:
        indicators = {}
        if isinstance(technical_result, dict):
            if 'body' in technical_result:
                body = json.loads(technical_result['body']) if isinstance(technical_result['body'], str) else technical_result['body']
                indicators = body.get('indicators', {})
            else:
                indicators = technical_result.get('indicators', {})

        bb_upper = float(indicators.get('bb_upper', 0))
        bb_lower = float(indicators.get('bb_lower', 0))
        current_price = float(indicators.get('current_price', 0))

        if current_price > 0 and bb_upper > bb_lower:
            return (bb_upper - bb_lower) / current_price
    except Exception as e:
        print(f"BB width extraction error: {e}")

    return BASELINE_BB_WIDTH  # デフォルト


def extract_indicator(technical_result: dict, key: str, default: float = 0.0) -> float:
    """テクニカル結果から任意のindicator値を抽出"""
    try:
        indicators = {}
        if isinstance(technical_result, dict):
            if 'body' in technical_result:
                body = json.loads(technical_result['body']) if isinstance(technical_result['body'], str) else technical_result['body']
                indicators = body.get('indicators', {})
            else:
                indicators = technical_result.get('indicators', {})
        return float(indicators.get(key, default))
    except Exception as e:
        print(f"Indicator extraction error for {key}: {e}")
        return default


def fetch_market_context() -> dict:
    """
    DynamoDBからマーケットコンテキストの最新データを取得
    market-context Lambda が30分間隔で書き込む

    Returns: {'market_score': float, 'fng_value': int, 'fng_score': float, ...}
             エラー/データなし時は空dict
    """
    try:
        table = dynamodb.Table(MARKET_CONTEXT_TABLE)
        response = table.query(
            KeyConditionExpression='context_type = :ct',
            ExpressionAttributeValues={':ct': 'global'},
            ScanIndexForward=False,  # 最新から
            Limit=1
        )
        items = response.get('Items', [])
        if items:
            item = items[0]
            age_seconds = int(time.time()) - int(item.get('timestamp', 0))
            # 2時間以上前のデータは古すぎる → 中立扱い
            if age_seconds > 7200:
                print(f"Market context data too old ({age_seconds}s ago), using neutral")
                return {}
            print(f"Market context: score={float(item.get('market_score', 0)):+.4f}, "
                  f"F&G={item.get('fng_value', '?')}/{item.get('fng_classification', '?')}, "
                  f"age={age_seconds}s")
            return item
        else:
            print("No market context data found in DynamoDB")
            return {}
    except Exception as e:
        print(f"Error fetching market context: {e}")
        import traceback
        traceback.print_exc()
        return {}


# Fear & Greed 連動 BUY閾値補正
# Extreme Fear (F&G < 20) ではBUY閾値を引き上げ、安易な逆張りを抑制
# Extreme Greed (F&G > 80) でもBUY閾値を引き上げ、天井掴みを防止
FNG_FEAR_THRESHOLD = 20    # これ以下で BUY 閾値引き上げ
FNG_GREED_THRESHOLD = 80   # これ以上で BUY 閾値引き上げ
FNG_BUY_MULTIPLIER_FEAR = 1.35   # Extreme Fear: BUY閾値を1.35倍（例: 0.28→0.378）
FNG_BUY_MULTIPLIER_GREED = 1.20  # Extreme Greed: BUY閾値を1.20倍


def calculate_per_currency_thresholds(scored_pairs: list, market_context: dict = None) -> dict:
    """
    通貨別ボラティリティ適応型閾値を計算（Fear & Greed 連動補正付き）

    各通貨のBB幅（ボラティリティ）に基づいて個別の閾値を計算する。
    高ボラ通貨（DOGE, SOLなど）は閾値を厳しく（ノイズに反応しない）、
    低ボラ通貨（BTCなど）は閾値を緩く（小さな確実なシグナルを拾う）設定。

    F&G連動補正は全通貨共通で適用（BUYのみ）:
    - Extreme Fear (< 20): BUY閾値を1.35倍に引き上げ
    - Extreme Greed (> 80): BUY閾値を1.20倍に引き上げ
    - SELL閾値は変更しない（損切りは市場環境に関わらず実行すべき）

    Returns: dict[pair] = {'buy': float, 'sell': float, 'vol_ratio': float}
    """
    if not scored_pairs:
        return {}

    # --- Fear & Greed 連動 BUY閾値補正（全通貨共通） ---
    fng_multiplier = 1.0
    fng_reason = ''
    if market_context:
        fng_value = int(market_context.get('fng_value', 50))
        if fng_value <= FNG_FEAR_THRESHOLD:
            fng_multiplier = FNG_BUY_MULTIPLIER_FEAR
            fng_reason = f'ExtremeFear(F&G={fng_value}<=20)'
        elif fng_value >= FNG_GREED_THRESHOLD:
            fng_multiplier = FNG_BUY_MULTIPLIER_GREED
            fng_reason = f'ExtremeGreed(F&G={fng_value}>=80)'

    thresholds = {}
    for scored in scored_pairs:
        pair = scored['pair']
        bb_width = scored.get('bb_width', BASELINE_BB_WIDTH)

        vol_ratio = bb_width / BASELINE_BB_WIDTH
        vol_ratio = max(VOL_CLAMP_MIN, min(VOL_CLAMP_MAX, vol_ratio))

        buy_t = BASE_BUY_THRESHOLD * vol_ratio * fng_multiplier
        sell_t = BASE_SELL_THRESHOLD * vol_ratio

        thresholds[pair] = {
            'buy': round(buy_t, 4),
            'sell': round(sell_t, 4),
            'vol_ratio': round(vol_ratio, 3),
        }

        name = TRADING_PAIRS.get(pair, {}).get('name', pair)
        print(f"  {name}({pair}) threshold: BUY={buy_t:+.4f} SELL={sell_t:+.4f} "
              f"(bb_width={bb_width:.4f}, vol_ratio={vol_ratio:.2f})")

    if fng_reason:
        print(f"  F&G correction: multiplier={fng_multiplier:.2f} [{fng_reason}]")

    return thresholds


def decide_per_currency_signals(scored_pairs: list,
                                 thresholds_map: dict) -> list:
    """
    通貨毎のBUY/SELL/HOLDを判定（通貨別閾値・ポジション非依存）

    各通貨のボラティリティに応じた個別閾値を使用して判定する。
    現在のポジション状況に関わらず、純粋にスコアと閾値で判定する。
    実際の注文可否はorder-executorが残高・ポジションを確認して決定する。

    Args:
        scored_pairs: score_pair()の結果リスト
        thresholds_map: {pair: {'buy': float, 'sell': float}} 通貨別閾値

    Returns: list of {pair, analysis_pair, signal, score, buy_threshold, sell_threshold, ...}
    """
    decisions = []
    for scored in scored_pairs:
        pair = scored['pair']
        coincheck_pair = TRADING_PAIRS.get(pair, {}).get('coincheck', pair)
        score = scored['total_score']

        pair_th = thresholds_map.get(pair, {'buy': BASE_BUY_THRESHOLD, 'sell': BASE_SELL_THRESHOLD})
        buy_t = pair_th['buy']
        sell_t = pair_th['sell']

        if score >= buy_t:
            signal = 'BUY'
        elif score <= sell_t:
            signal = 'SELL'
        else:
            signal = 'HOLD'

        print(f"  {pair} ({coincheck_pair}): score={score:+.4f} → {signal} "
              f"(BUY>={buy_t:+.4f}, SELL<={sell_t:+.4f})")

        decisions.append({
            'pair': coincheck_pair,
            'analysis_pair': pair,
            'signal': signal,
            'score': score,
            'components': scored.get('components', {}),
            'weights': scored.get('weights', {}),
            'chronos_confidence': scored.get('chronos_confidence', 0.5),
            'bb_width': scored.get('bb_width', 0),
            'buy_threshold': buy_t,
            'sell_threshold': sell_t,
        })

    buy_count = sum(1 for d in decisions if d['signal'] == 'BUY')
    sell_count = sum(1 for d in decisions if d['signal'] == 'SELL')
    hold_count = sum(1 for d in decisions if d['signal'] == 'HOLD')
    print(f"Per-currency signals: BUY={buy_count} SELL={sell_count} HOLD={hold_count}")

    return decisions


def find_all_active_positions() -> list:
    """全通貨のアクティブポジションを全て検索"""
    table = dynamodb.Table(POSITIONS_TABLE)
    positions = []

    for pair, config in TRADING_PAIRS.items():
        coincheck_pair = config['coincheck']
        try:
            pos = get_active_position(coincheck_pair)
            if pos:
                positions.append(pos)
        except Exception as e:
            print(f"Error checking position for {coincheck_pair}: {e}")

    return positions


def extract_score(result: dict, key: str, default: float) -> float:
    """結果からスコアを抽出"""
    try:
        if isinstance(result, dict):
            if 'body' in result:
                try:
                    body = json.loads(result['body']) if isinstance(result['body'], str) else result['body']
                    return float(body.get(key, default))
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    print(f"Warning: Failed to parse body for {key}: {e}")
            return float(result.get(key, default))
        return default
    except Exception as e:
        print(f"Error extracting score for {key}: {e}")
        return default


def safe_decimal(value: float, precision: int = 4) -> Decimal:
    """安全なDecimal変換（精度誤差対策）"""
    try:
        return Decimal(str(round(value, precision)))
    except Exception as e:
        print(f"Decimal conversion error for {value}: {e}")


def to_dynamo_map(data: dict) -> dict:
    """Python dictをDynamoDB互換のmap型に再帰変換（float→Decimal）"""
    result = {}
    for k, v in data.items():
        if isinstance(v, float):
            result[k] = safe_decimal(v)
        elif isinstance(v, dict):
            result[k] = to_dynamo_map(v)
        elif isinstance(v, list):
            result[k] = [to_dynamo_map(i) if isinstance(i, dict)
                         else safe_decimal(i) if isinstance(i, float)
                         else i
                         for i in v]
        else:
            result[k] = v
    return result


def _extract_raw_indicators(technical_result: dict) -> dict:
    """テクニカル結果から主要指標の生データを抽出"""
    try:
        indicators = {}
        if isinstance(technical_result, dict):
            if 'body' in technical_result:
                body = json.loads(technical_result['body']) if isinstance(technical_result['body'], str) else technical_result['body']
                indicators = body.get('indicators', {})
            else:
                indicators = technical_result.get('indicators', {})

        # 必要なキーのみ抽出（保存サイズ制御）
        keep_keys = ['rsi', 'macd', 'macd_signal', 'macd_histogram', 'macd_histogram_slope',
                     'sma_20', 'bb_upper', 'bb_lower', 'adx', 'regime',
                     'current_price', 'volume_multiplier', 'sma_200', 'golden_cross']
        return {k: indicators[k] for k in keep_keys if k in indicators}
    except Exception as e:
        print(f"Raw indicators extraction error: {e}")
        return {}


def _extract_chronos_detail(chronos_result: dict) -> dict:
    """Chronos予測の詳細を抽出（予測変化率を算出）"""
    try:
        cr = chronos_result
        if isinstance(cr, dict) and 'body' in cr:
            cr = json.loads(cr['body']) if isinstance(cr['body'], str) else cr['body']
        if not isinstance(cr, dict):
            return {}

        detail = {
            'confidence': float(cr.get('confidence', 0.5)),
            'model': cr.get('model', 'unknown'),
        }

        current = float(cr.get('current_price', 0))
        prediction = cr.get('prediction')
        if prediction and current > 0 and isinstance(prediction, list):
            avg_pred = sum(prediction) / len(prediction)
            detail['predicted_change_pct'] = round((avg_pred - current) / current * 100, 3)
            q10 = cr.get('prediction_q10')
            q90 = cr.get('prediction_q90')
            if q10 and isinstance(q10, list):
                detail['q10_change_pct'] = round((sum(q10)/len(q10) - current) / current * 100, 3)
            if q90 and isinstance(q90, list):
                detail['q90_change_pct'] = round((sum(q90)/len(q90) - current) / current * 100, 3)

        return detail
    except Exception as e:
        print(f"Chronos detail extraction error: {e}")
        return {}


def _extract_news_headlines(sentiment_result: dict) -> list:
    """センチメント結果からニュースヘッドライン上位を抽出"""
    try:
        sr = sentiment_result
        if isinstance(sr, dict) and 'body' in sr:
            sr = json.loads(sr['body']) if isinstance(sr['body'], str) else sr['body']
        if isinstance(sr, dict):
            return sr.get('top_headlines', [])
        return []
    except Exception as e:
        print(f"News headlines extraction error: {e}")
        return []
        return Decimal('0')


def generate_ai_comment(scored: dict, thresholds: dict) -> str:
    """Bedrock (Nova Micro) で総合評価コメントを日本語で生成"""
    try:
        pair = scored.get('pair', 'unknown')
        coin_name = TRADING_PAIRS.get(pair, {}).get('name', pair.upper())
        comp = scored.get('components', {})
        total = scored.get('total_score', 0)

        # シグナル判定
        signal = 'HOLD'
        if total >= thresholds.get('buy', BASE_BUY_THRESHOLD):
            signal = 'BUY'
        elif total <= thresholds.get('sell', BASE_SELL_THRESHOLD):
            signal = 'SELL'

        # 根拠データ
        ind = scored.get('indicators_detail', {})
        chr_d = scored.get('chronos_detail', {})
        news = scored.get('news_headlines', [])
        mkt = scored.get('market_context_detail', {})

        # プロンプトに渡す材料
        materials = f"""通貨: {coin_name}
総合スコア: {total:+.3f} (シグナル: {signal})
テクニカル: {comp.get('technical', 0):+.3f} (RSI={ind.get('rsi', 'N/A')}, ADX={ind.get('adx', 'N/A')}, レジーム={ind.get('regime', 'N/A')})
AI予測: {comp.get('chronos', 0):+.3f} (変化率={chr_d.get('predicted_change_pct', 'N/A')}%, 確信度={chr_d.get('confidence', 'N/A')})
センチメント: {comp.get('sentiment', 0):+.3f}
市場環境: {comp.get('market_context', 0):+.3f} (F&G={mkt.get('fng_value', 'N/A')}, BTC Dom={mkt.get('btc_dominance', 'N/A')}%)"""

        if news:
            headlines = '\n'.join(f"  - {n.get('title', '')} (score: {n.get('score', 0.5)})" for n in news[:3])
            materials += f"\n主要ニュース:\n{headlines}"

        prompt = f"""あなたは仮想通貨のアナリストです。以下の分析データから、個人投資家向けに2-3文の簡潔な日本語コメントを生成してください。

{materials}

ルール:
- 敬体（です・ます調）で書く
- データに基づいた客観的な分析を述べる
- 最も影響力の大きい要因を強調する
- 「買い推奨」「売り推奨」など直接的な投資助言は避ける
- 100文字以内に収める"""

        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 200, "temperature": 0.3},
        )

        comment = response['output']['message']['content'][0]['text'].strip()
        # 改行を除去して1行にする
        comment = comment.replace('\n', ' ').strip()
        # 長すぎる場合は切り詰め
        if len(comment) > 200:
            comment = comment[:197] + '...'

        tokens_in = response.get('usage', {}).get('inputTokens', 0)
        tokens_out = response.get('usage', {}).get('outputTokens', 0)
        print(f"AI comment for {pair}: {comment} (tokens: in={tokens_in}, out={tokens_out})")
        return comment

    except Exception as e:
        print(f"AI comment generation failed for {scored.get('pair', '?')}: {e}")
        traceback.print_exc()
        return ''


def save_signal(scored: dict, buy_threshold: float, sell_threshold: float):
    """全通貨のシグナルを保存（分析履歴・動的閾値対応）"""
    try:
        table = dynamodb.Table(SIGNALS_TABLE)
        # 5分区切りに丸めて重複保存を防止（手動再実行時に上書き）
        now = int(time.time())
        timestamp = now - (now % 300)

        signal = 'HOLD'
        if scored['total_score'] >= buy_threshold:
            signal = 'BUY'
        elif scored['total_score'] <= sell_threshold:
            signal = 'SELL'

        item = {
            'pair': scored['pair'],
            'timestamp': timestamp,
            'score': safe_decimal(scored['total_score']),
            'signal': signal,
            'technical_score': safe_decimal(scored['components']['technical']),
            'chronos_score': safe_decimal(scored['components']['chronos']),
            'sentiment_score': safe_decimal(scored['components']['sentiment']),
            'market_context_score': safe_decimal(scored['components'].get('market_context', 0)),
            'buy_threshold': safe_decimal(buy_threshold),
            'sell_threshold': safe_decimal(sell_threshold),
            'bb_width': safe_decimal(scored.get('bb_width', BASELINE_BB_WIDTH), 6),
            'ttl': timestamp + 7776000  # 90日後に削除
        }

        # 根拠データ（シグナル解説用）
        indicators = scored.get('indicators_detail', {})
        if indicators:
            item['indicators'] = to_dynamo_map(indicators)

        chronos_detail = scored.get('chronos_detail', {})
        if chronos_detail:
            item['chronos_detail'] = to_dynamo_map(chronos_detail)

        news_headlines = scored.get('news_headlines', [])
        if news_headlines:
            item['news_headlines'] = to_dynamo_map({'h': news_headlines[:3]})['h']

        market_detail = scored.get('market_context_detail', {})
        if market_detail:
            item['market_detail'] = to_dynamo_map(market_detail)

        ai_comment = scored.get('ai_comment', '')
        if ai_comment:
            item['ai_comment'] = ai_comment

        table.put_item(Item=item)
    except Exception as e:
        print(f"Error saving signal for {scored.get('pair', 'unknown')}: {e}")


def send_batch_order_message(decisions: list, timestamp: int):
    """SQSにバッチ注文メッセージ送信（全通貨の判定を1メッセージで・通貨別閾値付き）"""
    try:
        orders = []
        for d in decisions:
            order = {
                'pair': d['pair'],
                'signal': d['signal'],
                'score': d['score'],
                'analysis_context': {
                    'components': d.get('components', {}),
                    'bb_width': d.get('bb_width', 0),
                    'buy_threshold': round(d.get('buy_threshold', BASE_BUY_THRESHOLD), 4),
                    'sell_threshold': round(d.get('sell_threshold', BASE_SELL_THRESHOLD), 4),
                    'weights': d.get('weights', {}),
                    'chronos_confidence': d.get('chronos_confidence', 0.5),
                }
            }
            orders.append(order)

        message = {
            'batch': True,
            'timestamp': timestamp,
            'orders': orders
        }

        sqs.send_message(
            QueueUrl=ORDER_QUEUE_URL,
            MessageBody=json.dumps(message)
        )
        signals = [f"{d['signal']} {d['pair']}" for d in decisions]
        print(f"Batch order message sent to SQS: {', '.join(signals)}")
    except Exception as e:
        print(f"Error sending batch order message: {e}")


def notify_slack(result: dict, scored_pairs: list, active_positions: list,
                 thresholds_map: dict = None,
                 per_currency_decisions: list = None):
    """Slackに分析結果を通知（通貨別判定 + ランキング + 通貨別閾値 + 含み損益表示）"""
    thresholds_map = thresholds_map or {}
    if not SLACK_WEBHOOK_URL:
        return

    try:
        # 通貨別判定マップ
        decision_map = {}
        if per_currency_decisions:
            for d in per_currency_decisions:
                decision_map[d.get('analysis_pair', '')] = d['signal']

        # 判定サマリー
        summary = result.get('summary', {})
        buy_count = summary.get('buy', 0)
        sell_count = summary.get('sell', 0)
        hold_count = summary.get('hold', 0)

        if buy_count > 0 or sell_count > 0:
            parts = []
            if buy_count > 0:
                parts.append(f"BUY {buy_count}件")
            if sell_count > 0:
                parts.append(f"SELL {sell_count}件")
            if hold_count > 0:
                parts.append(f"HOLD {hold_count}件")
            header_text = f"📊 マルチ通貨分析: {' / '.join(parts)}"
        else:
            header_text = "⚪ マルチ通貨分析: ALL HOLD"

        # スコアバー
        def score_bar(score):
            pos = int((score + 1) * 5)
            pos = max(0, min(10, pos))
            return '▓' * pos + '░' * (10 - pos)

        # ランキング表示（通貨別判定付き）
        ranking_text = ""
        for i, s in enumerate(scored_pairs):
            name = TRADING_PAIRS.get(s['pair'], {}).get('name', s['pair'])
            medal = ['🥇', '🥈', '🥉'][i] if i < 3 else f'{i+1}.'
            weights = s.get('weights', {})

            # 通貨別判定表示
            pair_signal = decision_map.get(s['pair'], 'HOLD')
            signal_emoji = {'BUY': '🟢BUY', 'SELL': '🔴SELL', 'HOLD': '⚪HOLD'}.get(pair_signal, '⚪HOLD')

            # 通貨別閾値
            pair_th = thresholds_map.get(s['pair'], {'buy': BASE_BUY_THRESHOLD, 'sell': BASE_SELL_THRESHOLD})

            ranking_text += (
                f"{medal} *{name}*: `{s['total_score']:+.4f}` {score_bar(s['total_score'])} → {signal_emoji}\n"
                f"    Tech: `{s['components']['technical']:+.3f}`({weights.get('technical', TECHNICAL_WEIGHT):.2f}) | "
                f"AI: `{s['components']['chronos']:+.3f}`({weights.get('chronos', CHRONOS_WEIGHT):.2f}) | "
                f"Sent: `{s['components']['sentiment']:+.3f}` | "
                f"Mkt: `{s['components'].get('market_context', 0):+.3f}`\n"
                f"    閾値: BUY≥`{pair_th['buy']:+.3f}` / SELL≤`{pair_th['sell']:+.3f}`\n"
            )

        # ポジション情報（複数対応 + 含み損益表示）
        position_text = ""
        if active_positions:
            total_unrealized = 0
            position_lines = []
            for pos in active_positions:
                pos_pair = pos.get('pair', '?')
                entry_price = float(pos.get('entry_price', 0))
                amount = float(pos.get('amount', 0))

                # 通貨名を取得
                pos_name = pos_pair
                for pair_key, config in TRADING_PAIRS.items():
                    if config['coincheck'] == pos_pair:
                        pos_name = config['name']
                        break

                # 現在価格をCoincheck APIから取得（JPY建て）
                current_price = 0
                try:
                    current_price = get_current_price(pos_pair)
                except Exception as e:
                    print(f"Failed to get current price for {pos_pair}: {e}")

                # 保有時間
                entry_time = int(pos.get('entry_time', 0))
                hold_elapsed = int(time.time()) - entry_time if entry_time else 0
                hold_min = hold_elapsed // 60
                hold_status = f" | 保有{hold_min}分"

                if entry_price > 0 and current_price > 0:
                    pnl = (current_price - entry_price) * amount
                    pnl_pct = (current_price - entry_price) / entry_price * 100
                    total_unrealized += pnl
                    pnl_emoji = '📈' if pnl >= 0 else '📉'
                    position_lines.append(
                        f"{pnl_emoji} *{pos_name}* (`{pos_pair}`)\n"
                        f"    参入: ¥{entry_price:,.0f} → 現在: ¥{current_price:,.0f} | "
                        f"P/L: `¥{pnl:+,.0f}` (`{pnl_pct:+.2f}%`){hold_status}"
                    )
                else:
                    position_lines.append(
                        f"📍 *{pos_name}* (`{pos_pair}`) 参入: ¥{entry_price:,.0f}{hold_status}"
                    )

            position_text = '\n'.join(position_lines)
            if len(active_positions) > 1:
                total_emoji = '💰' if total_unrealized >= 0 else '💸'
                position_text += f"\n{total_emoji} *合計含み損益: `¥{total_unrealized:+,.0f}`*"
        else:
            position_text = "なし"

        # マーケットコンテキスト情報
        mkt_detail = scored_pairs[0].get('market_context_detail', {}) if scored_pairs else {}
        if mkt_detail:
            fng_val = mkt_detail.get('fng_value', '?')
            fng_cls = mkt_detail.get('fng_classification', '?')
            btc_dom = mkt_detail.get('btc_dominance', 0)
            mkt_text = (
                f"F&G: `{fng_val}` ({fng_cls}) | "
                f"BTC Dom: `{btc_dom:.1f}%` | "
                f"Scores: F&G=`{mkt_detail.get('fng_score', 0):+.3f}` "
                f"Fund=`{mkt_detail.get('funding_score', 0):+.3f}` "
                f"Dom=`{mkt_detail.get('dominance_score', 0):+.3f}`"
            )
        else:
            mkt_text = "データなし（中立扱い）"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": header_text,
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🌍 市場環境*\n{mkt_text}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📊 通貨ランキング（期待値順）*\n{ranking_text}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*💼 ポジション ({len(active_positions)}件)*\n{position_text}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"基準閾値: BUY≥`{BASE_BUY_THRESHOLD:+.3f}` / SELL≤`{BASE_SELL_THRESHOLD:+.3f}` (通貨別ボラ補正あり) | "
                                                f"基準重み: Tech={TECHNICAL_WEIGHT} AI={CHRONOS_WEIGHT}(確信度で±0.08変動) Sent={SENTIMENT_WEIGHT} Mkt={MARKET_CONTEXT_WEIGHT}"
                                                + (f" | ⚠️ F&G補正あり" if any(th['buy'] > BASE_BUY_THRESHOLD * 1.3 for th in thresholds_map.values()) else "")}
                ]
            }
        ]

        if buy_count > 0 or sell_count > 0:
            action_pairs = [f"{d['signal']} {TRADING_PAIRS.get(d.get('analysis_pair', ''), {}).get('name', d['pair'])}"
                           for d in (per_currency_decisions or []) if d['signal'] != 'HOLD']
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚡ *注文キューに送信済み*: {', '.join(action_pairs)}"
                }
            })

        message = {"blocks": blocks}

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(message).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=10)
        print(f"Slack notification sent (status: {response.status})")

    except Exception as e:
        print(f"Slack notification failed: {e}")
        import traceback
        traceback.print_exc()
