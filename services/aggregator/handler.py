"""
アグリゲーター Lambda (マルチタイムフレーム対応 / デュアルモード)

モード1: tf_score (各TFのStep Functions終了時に呼ばれる)
  - テクニカル + Chronos + センチメントのスコアを統合
  - 通貨別にper-TFスコアを計算
  - tf-scores DynamoDBテーブルに保存

モード2: meta_aggregate (15分間隔でEventBridgeから直接呼ばれる)
  - 全TFのスコアをDynamoDBから読み取り
  - マルチTFウェイトで加重平均
  - TF間整合性チェック（方向性の一致度）
  - 通貨毎にBUY/SELL/HOLD判定
  - Slack通知 + DynamoDB保存
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
    TIMEFRAME_CONFIG, ACTIVE_TIMEFRAMES, TIMEFRAME_WEIGHTS,
    TF_SCORES_TABLE, make_pair_tf_key,
    get_current_price, get_active_position, send_slack_notification, dynamodb
)

bedrock = boto3.client('bedrock-runtime')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'apac.amazon.nova-micro-v1:0')

SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
MARKET_CONTEXT_TABLE = os.environ.get('MARKET_CONTEXT_TABLE', 'eth-trading-market-context')

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

# マルチTF整合性チェック
TF_ALIGNMENT_BONUS = 1.15    # 75%以上同方向 → 15%増幅
TF_MISALIGN_PENALTY = 0.85   # 50%以下同方向 → 15%減衰

# TFスコア鮮度チェック（基準の2倍以上古いデータは使わない）
TF_STALENESS = {
    "15m": 20 * 60,      # 20分以上で陳腐
    "1h":  75 * 60,      # 75分以上
    "4h":  5 * 3600,     # 5時間以上
    "1d":  26 * 3600,    # 26時間以上
}


def handler(event, context):
    """デュアルモードルーター"""
    mode = event.get('mode', 'tf_score')

    if mode == 'meta_aggregate':
        return handle_meta_aggregate(event, context)
    else:
        return handle_tf_score(event, context)


# =============================================================================
# Mode 1: TF Score (各TFのStep Functions終了時)
# =============================================================================

def handle_tf_score(event, context):
    """
    各TF分析ワークフローの最終ステップ。
    Step Functions Map の出力 (tech_sent_results + chronos_results) をマージし、
    per-TFスコアを計算して tf-scores テーブルに保存する。
    """
    timeframe = event.get('timeframe', '1h')
    pairs = event.get('pairs', list(TRADING_PAIRS.keys()))
    tech_sent_results = event.get('tech_sent_results', [])
    chronos_results = event.get('chronos_results', [])

    # 後方互換: 旧形式 (analysis_results)
    if not tech_sent_results and 'analysis_results' in event:
        pairs_results = event['analysis_results']
        return _handle_legacy_tf_score(pairs_results, timeframe)

    print(f"[tf_score] Scoring {len(pairs)} pairs for timeframe={timeframe}")

    try:
        # tech_sent_results と chronos_results をペアインデックスでマージ
        merged_results = []
        for i, pair in enumerate(pairs):
            result = {'pair': pair}
            if i < len(tech_sent_results):
                tsr = tech_sent_results[i]
                result['technical'] = tsr.get('technical', {})
                result['sentiment'] = tsr.get('sentiment', {})
            if i < len(chronos_results):
                result['chronos'] = chronos_results[i]
            merged_results.append(result)

        # マーケットコンテキスト取得（per-TFスコアに市場環境を反映）
        market_context = fetch_market_context()

        # 各通貨のper-TFスコアを計算（MarketContext含む4成分）
        scored_pairs = []
        for result in merged_results:
            pair = result.get('pair', 'unknown')
            scored = score_pair(pair, result, market_context=market_context)
            scored['timeframe'] = timeframe
            scored_pairs.append(scored)

        # per-TF BUY/SELL/HOLD判定（TF別BBベースラインでボラ補正、F&Gなし）
        tf_bb_baseline = TIMEFRAME_CONFIG.get(timeframe, {}).get('bb_baseline', BASELINE_BB_WIDTH)
        for scored in scored_pairs:
            bb_width = scored.get('bb_width', tf_bb_baseline)
            vol_ratio = max(VOL_CLAMP_MIN, min(VOL_CLAMP_MAX, bb_width / tf_bb_baseline))
            buy_t = BASE_BUY_THRESHOLD * vol_ratio
            sell_t = BASE_SELL_THRESHOLD * vol_ratio
            if scored['total_score'] >= buy_t:
                scored['signal'] = 'BUY'
            elif scored['total_score'] <= sell_t:
                scored['signal'] = 'SELL'
            else:
                scored['signal'] = 'HOLD'
            scored['buy_threshold'] = round(buy_t, 4)
            scored['sell_threshold'] = round(sell_t, 4)

        # tf-scores DynamoDBテーブルに保存
        timestamp = int(time.time())
        for scored in scored_pairs:
            _save_tf_score(scored, timeframe, timestamp)

        print(f"[tf_score] Saved {len(scored_pairs)} TF scores for {timeframe}")

        return {
            'statusCode': 200,
            'mode': 'tf_score',
            'timeframe': timeframe,
            'scores': [
                {'pair': s['pair'], 'total_score': round(s['total_score'], 4)}
                for s in scored_pairs
            ]
        }

    except Exception as e:
        print(f"[tf_score] Error: {str(e)}")
        traceback.print_exc()
        return {'statusCode': 500, 'mode': 'tf_score', 'error': str(e)}


def _handle_legacy_tf_score(pairs_results, timeframe):
    """後方互換: 旧形式の analysis_results をスコアリング"""
    scored_pairs = []
    for result in pairs_results:
        pair = result.get('pair', 'unknown')
        scored = score_pair(pair, result, market_context=None)
        scored['timeframe'] = timeframe
        scored_pairs.append(scored)

    timestamp = int(time.time())
    for scored in scored_pairs:
        _save_tf_score(scored, timeframe, timestamp)

    return {
        'statusCode': 200,
        'mode': 'tf_score',
        'timeframe': timeframe,
        'scores': [
            {'pair': s['pair'], 'total_score': round(s['total_score'], 4)}
            for s in scored_pairs
        ]
    }


def _save_tf_score(scored: dict, timeframe: str, timestamp: int):
    """per-TFスコアをDynamoDBに保存"""
    try:
        table = dynamodb.Table(TF_SCORES_TABLE)
        pair = scored['pair']
        pair_tf_key = make_pair_tf_key(pair, timeframe)

        item = {
            'pair_tf': pair_tf_key,
            'timestamp': timestamp,
            'pair': pair,
            'timeframe': timeframe,
            'total_score': safe_decimal(scored['total_score']),
            'components': to_dynamo_map(scored.get('components', {})),
            'weights': to_dynamo_map(scored.get('weights', {})),
            'chronos_confidence': safe_decimal(scored.get('chronos_confidence', 0.5)),
            'bb_width': safe_decimal(scored.get('bb_width', BASELINE_BB_WIDTH), 6),
            'ttl': timestamp + 86400,  # 24時間で期限切れ
        }

        indicators = scored.get('indicators_detail', {})
        if indicators:
            item['indicators'] = to_dynamo_map(indicators)

        chronos_detail = scored.get('chronos_detail', {})
        if chronos_detail:
            item['chronos_detail'] = to_dynamo_map(chronos_detail)

        news_headlines = scored.get('news_headlines', [])
        if news_headlines:
            item['news_headlines'] = to_dynamo_map({'h': news_headlines[:5]})['h']

        market_detail = scored.get('market_context_detail', {})
        if market_detail:
            item['market_detail'] = to_dynamo_map(market_detail)

        # per-TF BUY/SELL/HOLDシグナル
        item['signal'] = scored.get('signal', 'HOLD')
        item['buy_threshold'] = safe_decimal(scored.get('buy_threshold', BASE_BUY_THRESHOLD))
        item['sell_threshold'] = safe_decimal(scored.get('sell_threshold', BASE_SELL_THRESHOLD))

        table.put_item(Item=item)
    except Exception as e:
        print(f"Error saving TF score for {scored.get('pair', '?')}@{timeframe}: {e}")


# =============================================================================
# Mode 2: Meta Aggregate (15分間隔でEventBridgeから直接呼ばれる)
# =============================================================================

def handle_meta_aggregate(event, context):
    """
    全TFのスコアを統合し、最終BUY/SELL/HOLD判定を行う。
    1. DynamoDBから全TFスコアを読み取り
    2. マーケットコンテキスト取得
    3. マルチTFウェイトで加重平均 + TF間整合性チェック
    4. 通貨別閾値計算 + BUY/SELL/HOLD判定
    5. AI総合コメント生成 + Slack通知 + DynamoDB保存
    """
    pairs = list(TRADING_PAIRS.keys())

    try:
        # 1. 全TFスコアを読み取り
        all_tf_scores = _read_all_tf_scores(pairs)

        if not all_tf_scores or all(not v for v in all_tf_scores.values()):
            print("[meta_aggregate] No TF scores found in DynamoDB")
            return {'signal': 'HOLD', 'has_signal': False, 'reason': 'no_tf_scores'}

        # 2. マーケットコンテキスト取得
        market_context = fetch_market_context()

        # 3. マルチTF加重平均 + 整合性チェック
        scored_pairs = []
        for pair in pairs:
            pair_scores = all_tf_scores.get(pair, {})
            multi_tf_result = _calculate_multi_tf_score(pair, pair_scores, market_context)
            scored_pairs.append(multi_tf_result)

        # 4. 通貨別閾値計算
        thresholds_map = calculate_per_currency_thresholds(scored_pairs, market_context)

        # 5. AI総合コメント + シグナル保存
        for scored in scored_pairs:
            pair_th = thresholds_map.get(scored['pair'],
                                         {'buy': BASE_BUY_THRESHOLD, 'sell': BASE_SELL_THRESHOLD})
            ai_comment = generate_ai_comment(scored, pair_th)
            scored['ai_comment'] = ai_comment
            save_signal(scored, pair_th['buy'], pair_th['sell'])

        # 6. スコア順ソート
        scored_pairs.sort(key=lambda x: x['total_score'], reverse=True)

        # 7. BUY/SELL/HOLD判定
        per_currency_decisions = decide_per_currency_signals(scored_pairs, thresholds_map)

        # 8. ポジション取得
        actionable_decisions = [d for d in per_currency_decisions if d['signal'] != 'HOLD']
        has_signal = len(actionable_decisions) > 0
        active_positions = find_all_active_positions()

        buy_decisions = [d for d in per_currency_decisions if d['signal'] == 'BUY']
        sell_decisions = [d for d in per_currency_decisions if d['signal'] == 'SELL']
        hold_decisions = [d for d in per_currency_decisions if d['signal'] == 'HOLD']

        result = {
            'mode': 'meta_aggregate',
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
            'thresholds': {pair: {'buy': th['buy'], 'sell': th['sell']}
                           for pair, th in thresholds_map.items()},
            'timestamp': int(time.time())
        }

        # 10. Slack通知
        notify_slack(result, scored_pairs, active_positions,
                     thresholds_map, per_currency_decisions)

        return result

    except Exception as e:
        print(f"[meta_aggregate] Error: {str(e)}")
        traceback.print_exc()
        return {'signal': 'HOLD', 'has_signal': False, 'error': str(e)}


def _read_all_tf_scores(pairs: list) -> dict:
    """
    全通貨 × 全TFの最新スコアをDynamoDBから読み取り

    Returns: {"btc_usdt": {"15m": {...}, "1h": {...}, ...}, ...}
    """
    table = dynamodb.Table(TF_SCORES_TABLE)
    current_time = int(time.time())
    result = {}

    for pair in pairs:
        result[pair] = {}
        for tf in ACTIVE_TIMEFRAMES:
            pair_tf_key = make_pair_tf_key(pair, tf)
            try:
                response = table.query(
                    KeyConditionExpression='pair_tf = :ptf',
                    ExpressionAttributeValues={':ptf': pair_tf_key},
                    ScanIndexForward=False,
                    Limit=1
                )
                items = response.get('Items', [])
                if items:
                    item = items[0]
                    ts = int(item.get('timestamp', 0))
                    staleness = TF_STALENESS.get(tf, 3600)

                    if current_time - ts > staleness:
                        print(f"  {pair}@{tf}: stale ({current_time - ts}s > {staleness}s)")
                        continue

                    result[pair][tf] = {
                        'total_score': float(item.get('total_score', 0)),
                        'components': _dynamo_to_float(item.get('components', {})),
                        'weights': _dynamo_to_float(item.get('weights', {})),
                        'chronos_confidence': float(item.get('chronos_confidence', 0.5)),
                        'bb_width': float(item.get('bb_width', BASELINE_BB_WIDTH)),
                        'timestamp': ts,
                        'indicators': _dynamo_to_float(item.get('indicators', {})),
                        'chronos_detail': _dynamo_to_float(item.get('chronos_detail', {})),
                        'signal': item.get('signal', 'HOLD'),
                        'news_headlines': _dynamo_to_float(item.get('news_headlines', [])),
                        'market_detail': _dynamo_to_float(item.get('market_detail', {})),
                    }
                    age = current_time - ts
                    print(f"  {pair}@{tf}: score={result[pair][tf]['total_score']:+.4f} (age={age}s)")
                else:
                    print(f"  {pair}@{tf}: no data")
            except Exception as e:
                print(f"  {pair}@{tf}: read error: {e}")

    return result


def _dynamo_to_float(data):
    """DynamoDB Decimal → Python float 再帰変換"""
    if isinstance(data, dict):
        return {k: _dynamo_to_float(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_dynamo_to_float(i) for i in data]
    elif isinstance(data, Decimal):
        return float(data)
    return data


def _collect_news_from_tfs(available_tfs: dict) -> list:
    """各TFに保存されたニュースヘッドラインを収集・重複除去して返す。
    最新TF（タイムスタンプが大きい）のニュースを優先。"""
    seen_titles = set()
    all_news = []
    # タイムスタンプが新しいTF順で処理
    sorted_tfs = sorted(available_tfs.items(),
                        key=lambda x: x[1].get('timestamp', 0), reverse=True)
    for tf, data in sorted_tfs:
        for n in data.get('news_headlines', []):
            title = n.get('title', '') if isinstance(n, dict) else str(n)
            if title and title not in seen_titles:
                seen_titles.add(title)
                all_news.append(n)
    return all_news[:5]  # 最大5件


def _calculate_multi_tf_score(pair: str, pair_tf_scores: dict,
                               market_context: dict) -> dict:
    """
    マルチTF加重平均 + 整合性チェック + マーケットコンテキスト

    TFスコアは各TFのStep Functionsで計算済み（Tech + Chronos + Sentiment）。
    ここではTF間の加重平均 + 方向性一致チェック + MarketContextを加味して
    最終スコアを算出する。
    """
    available_tfs = {tf: data for tf, data in pair_tf_scores.items()
                     if tf in TIMEFRAME_WEIGHTS}

    if not available_tfs:
        return _neutral_scored_result(pair)

    # ウェイト再正規化（不足TFがある場合）
    total_weight = sum(TIMEFRAME_WEIGHTS[tf] for tf in available_tfs)
    norm_w = {tf: TIMEFRAME_WEIGHTS[tf] / total_weight for tf in available_tfs}

    # TF加重平均スコア
    weighted_score = sum(
        data['total_score'] * norm_w[tf] for tf, data in available_tfs.items()
    )

    # TF間方向性整合性チェック
    directions = [1 if data['total_score'] > 0.02 else
                  (-1 if data['total_score'] < -0.02 else 0)
                  for data in available_tfs.values()]
    positive = sum(1 for d in directions if d > 0)
    negative = sum(1 for d in directions if d < 0)
    majority = max(positive, negative)
    agreement = majority / len(directions) if directions else 0.5

    if agreement >= 0.75:
        weighted_score *= TF_ALIGNMENT_BONUS
        alignment = 'aligned'
    elif agreement <= 0.5:
        weighted_score *= TF_MISALIGN_PENALTY
        alignment = 'conflicting'
    else:
        alignment = 'mixed'

    # マーケットコンテキスト
    market_context_normalized = 0.0
    market_context_detail = {}
    alt_dominance_adjustment = 0.0
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
        if pair != 'btc_usdt':
            btc_dom = float(market_context.get('btc_dominance', 50))
            if btc_dom > 60:
                alt_dominance_adjustment = -0.05
            elif btc_dom < 40:
                alt_dominance_adjustment = 0.05

    # 最終スコア = TF加重平均 + alt調整
    # ※ MarketContextは各per-TFスコアに含まれているため、ここでは加算しない
    final_score = weighted_score + alt_dominance_adjustment
    final_score = max(-1.0, min(1.0, final_score))

    # BB幅の加重平均
    avg_bb = sum(data.get('bb_width', BASELINE_BB_WIDTH) * norm_w[tf]
                 for tf, data in available_tfs.items())

    # コンポーネントスコアの加重平均
    avg_components = {}
    for key in ['technical', 'chronos', 'sentiment', 'market_context']:
        avg_components[key] = round(sum(
            data.get('components', {}).get(key, 0) * norm_w[tf]
            for tf, data in available_tfs.items()
        ), 3)

    # Chronos確信度の加重平均
    avg_conf = sum(data.get('chronos_confidence', 0.5) * norm_w[tf]
                   for tf, data in available_tfs.items())

    # 代表TFのindicators（1h優先）
    rep_tf = '1h' if '1h' in available_tfs else list(available_tfs.keys())[0]
    indicators_detail = available_tfs[rep_tf].get('indicators', {})
    chronos_detail = available_tfs[rep_tf].get('chronos_detail', {})
    current_price_usd = indicators_detail.get('current_price', 0)

    print(f"  {pair}: multi-TF score={final_score:+.4f} "
          f"(alignment={alignment}, tfs={list(available_tfs.keys())})")

    return {
        'pair': pair,
        'total_score': final_score,
        'components': avg_components,
        'weights': {
            'technical': TECHNICAL_WEIGHT,
            'chronos': CHRONOS_WEIGHT,
            'sentiment': SENTIMENT_WEIGHT,
            'market_context': MARKET_CONTEXT_WEIGHT,
        },
        'chronos_confidence': round(avg_conf, 3),
        'market_context_detail': market_context_detail,
        'bb_width': avg_bb,
        'current_price_usd': current_price_usd,
        'indicators_detail': indicators_detail,
        'chronos_detail': chronos_detail,
        'news_headlines': _collect_news_from_tfs(available_tfs),
        'tf_breakdown': {
            tf: {
                'score': round(data['total_score'], 4),
                'weight': round(norm_w[tf], 3),
                'components': data.get('components', {}),
                'signal': data.get('signal', 'HOLD'),
            }
            for tf, data in available_tfs.items()
        },
        'alignment': alignment,
        'available_timeframes': list(available_tfs.keys()),
    }


def _neutral_scored_result(pair: str) -> dict:
    """データなし時の中立スコア結果"""
    return {
        'pair': pair,
        'total_score': 0.0,
        'components': {'technical': 0, 'chronos': 0, 'sentiment': 0, 'market_context': 0},
        'weights': {
            'technical': TECHNICAL_WEIGHT,
            'chronos': CHRONOS_WEIGHT,
            'sentiment': SENTIMENT_WEIGHT,
            'market_context': MARKET_CONTEXT_WEIGHT,
        },
        'chronos_confidence': 0.5,
        'market_context_detail': {},
        'bb_width': BASELINE_BB_WIDTH,
        'current_price_usd': 0,
        'indicators_detail': {},
        'chronos_detail': {},
        'news_headlines': [],
        'tf_breakdown': {},
        'alignment': 'unknown',
        'available_timeframes': [],
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
    # BTC自体はDominance上昇で有利、アルト（ETH, XRP）は不利
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


# Fear & Greed 連動 BUY閾値補正（加算方式）
# 旧方式（乗算）: buy_t = BASE × vol_ratio × fng_multiplier
#   → ボラ補正との二重効果で高ボラ通貨の閾値が0.6超になり事実上買えなくなる問題
# 新方式（加算）: buy_t = BASE × vol_ratio + fng_adder
#   → ボラ補正とF&G補正が独立し、本当に強いシグナルなら拾える
FNG_FEAR_THRESHOLD = 20    # これ以下で BUY 閾値引き上げ
FNG_GREED_THRESHOLD = 80   # これ以上で BUY 閾値引き上げ
FNG_BUY_ADDER_FEAR = 0.06    # Extreme Fear: BUY閾値に+0.06加算（例: 0.25→0.31）
FNG_BUY_ADDER_GREED = 0.04   # Extreme Greed: BUY閾値に+0.04加算
# BUY閾値の絶対上限（どんなに高ボラ+Extreme環境でもこれ以上にはならない）
BUY_THRESHOLD_CAP = float(os.environ.get('BUY_THRESHOLD_CAP', '0.45'))


def calculate_per_currency_thresholds(scored_pairs: list, market_context: dict = None) -> dict:
    """
    通貨別ボラティリティ適応型閾値を計算（Fear & Greed 連動補正付き）

    各通貨のBB幅（ボラティリティ）に基づいて個別の閾値を計算する。
    高ボラ通貨は閾値を厳しく（ノイズに反応しない）、
    低ボラ通貨（BTCなど）は閾値を緩く（小さな確実なシグナルを拾う）設定。
    BB baselineはTF別加重平均を使用（異なるTFのBB幅を統一基準で比較）。

    F&G連動補正は全通貨共通で適用（BUYのみ、加算方式）:
    - Extreme Fear (< 20): BUY閾値に+0.06加算（恐怖時の安易な逆張り抑制）
    - Extreme Greed (> 80): BUY閾値に+0.04加算（天井掴み防止）
    - SELL閾値は変更しない（損切りは市場環境に関わらず実行すべき）

    旧方式（乗算）ではボラ補正×F&G補正の二重効果で高ボラ通貨の閾値が
    0.6超に達し事実上買えなくなっていた。加算方式+上限キャップで解消。

    Returns: dict[pair] = {'buy': float, 'sell': float, 'vol_ratio': float}
    """
    if not scored_pairs:
        return {}

    # --- Fear & Greed 連動 BUY閾値補正（加算方式・全通貨共通） ---
    fng_adder = 0.0
    fng_reason = ''
    if market_context:
        fng_value = int(market_context.get('fng_value', 50))
        if fng_value <= FNG_FEAR_THRESHOLD:
            fng_adder = FNG_BUY_ADDER_FEAR
            fng_reason = f'ExtremeFear(F&G={fng_value}<=20)'
        elif fng_value >= FNG_GREED_THRESHOLD:
            fng_adder = FNG_BUY_ADDER_GREED
            fng_reason = f'ExtremeGreed(F&G={fng_value}>=80)'

    thresholds = {}
    for scored in scored_pairs:
        pair = scored['pair']
        bb_width = scored.get('bb_width', BASELINE_BB_WIDTH)

        # メタ集約レベルのBB baseline: 各TFのbb_baselineの加重平均
        # （異なるTFのBB幅を統一基準で比較するため）
        available_tfs = scored.get('available_timeframes', [])
        if available_tfs:
            tf_breakdown = scored.get('tf_breakdown', {})
            total_w = sum(tf_breakdown.get(tf, {}).get('weight', 0) for tf in available_tfs)
            if total_w > 0:
                meta_baseline = sum(
                    TIMEFRAME_CONFIG.get(tf, {}).get('bb_baseline', BASELINE_BB_WIDTH)
                    * tf_breakdown.get(tf, {}).get('weight', 0)
                    for tf in available_tfs
                ) / total_w
            else:
                meta_baseline = BASELINE_BB_WIDTH
        else:
            meta_baseline = BASELINE_BB_WIDTH

        vol_ratio = bb_width / meta_baseline
        vol_ratio = max(VOL_CLAMP_MIN, min(VOL_CLAMP_MAX, vol_ratio))

        # ボラ補正（乗算） + F&G補正（加算） + 上限キャップ
        buy_t = BASE_BUY_THRESHOLD * vol_ratio + fng_adder
        buy_t = min(buy_t, BUY_THRESHOLD_CAP)  # 絶対上限
        sell_t = BASE_SELL_THRESHOLD * vol_ratio

        thresholds[pair] = {
            'buy': round(buy_t, 4),
            'sell': round(sell_t, 4),
            'vol_ratio': round(vol_ratio, 3),
        }

        name = TRADING_PAIRS.get(pair, {}).get('name', pair)
        capped = ' [CAPPED]' if buy_t >= BUY_THRESHOLD_CAP - 0.001 else ''
        print(f"  {name}({pair}) threshold: BUY={buy_t:+.4f} SELL={sell_t:+.4f} "
              f"(bb_width={bb_width:.4f}, vol_ratio={vol_ratio:.2f}){capped}")

    if fng_reason:
        print(f"  F&G correction: adder={fng_adder:+.3f} [{fng_reason}]")

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


def _interpret_indicators(scored: dict) -> list:
    """テクニカル指標を定性的な洞察に変換"""
    ind = scored.get('indicators_detail', {})
    insights = []

    # RSI
    rsi = ind.get('rsi')
    if rsi is not None:
        rsi_val = float(rsi)
        if rsi_val > 70:
            insights.append(f"RSI={rsi_val:.0f}で買われすぎゾーン突入 → 利確圧力・反転リスク上昇")
        elif rsi_val > 60:
            insights.append(f"RSI={rsi_val:.0f}で買い勢力がやや優勢")
        elif rsi_val < 30:
            insights.append(f"RSI={rsi_val:.0f}で売られすぎゾーン → 反発期待")
        elif rsi_val < 40:
            insights.append(f"RSI={rsi_val:.0f}で売り勢力がやや優勢")
        else:
            insights.append(f"RSI={rsi_val:.0f}で中立圏")

    # ADX + レジーム
    adx = ind.get('adx')
    regime = ind.get('regime', 'neutral')
    if adx is not None:
        adx_val = float(adx)
        if adx_val > 40:
            insights.append(f"ADX={adx_val:.0f}: 非常に強いトレンドが発生中（{regime}相場）")
        elif adx_val > 25:
            insights.append(f"ADX={adx_val:.0f}: トレンドが明確（{regime}相場）")
        else:
            insights.append(f"ADX={adx_val:.0f}: 方向感が弱くレンジ気味")

    # MACD モメンタム
    macd_slope = ind.get('macd_histogram_slope')
    macd_hist = ind.get('macd_histogram')
    if macd_slope is not None and macd_hist is not None:
        slope = float(macd_slope)
        hist = float(macd_hist)
        if hist > 0 and slope > 0:
            insights.append("MACD: 強気モメンタム加速中")
        elif hist > 0 and slope < 0:
            insights.append("MACD: 強気だがモメンタム鈍化 → ピークアウトの兆し")
        elif hist < 0 and slope < 0:
            insights.append("MACD: 弱気モメンタム加速中")
        elif hist < 0 and slope > 0:
            insights.append("MACD: 弱気だが下げ勢い鈍化 → 底打ちの兆し")

    # BB位置（価格がバンドのどこにあるか）
    bb_upper = ind.get('bb_upper', 0)
    bb_lower = ind.get('bb_lower', 0)
    price = ind.get('current_price', 0)
    if price and bb_upper and bb_lower and float(bb_upper) > float(bb_lower):
        bb_range = float(bb_upper) - float(bb_lower)
        bb_pos = (float(price) - float(bb_lower)) / bb_range
        if bb_pos > 0.9:
            insights.append("価格がBB上限に接近（ブレイクアウトまたは反落の分岐点）")
        elif bb_pos < 0.1:
            insights.append("価格がBB下限に接近（反発またはブレイクダウンの分岐点）")

    # SMA200 長期トレンド & ゴールデンクロス
    sma_200 = ind.get('sma_200')
    golden_cross = ind.get('golden_cross')
    if sma_200 and price:
        if float(price) > float(sma_200):
            insights.append("SMA200の上を推移（長期上昇トレンド内）")
        else:
            insights.append("SMA200を下回る（長期下降トレンド内）")
    if golden_cross:
        insights.append("ゴールデンクロス発生中")

    # 出来高
    volume_mult = ind.get('volume_multiplier')
    if volume_mult is not None:
        vm = float(volume_mult)
        if vm > 2.0:
            insights.append(f"出来高が平均の{vm:.1f}倍 → 高い関心、シグナルの信頼度が高い")
        elif vm < 0.5:
            insights.append(f"出来高が平均の{vm:.1f}倍と低迷 → ブレイク方向の信頼度は低い")

    return insights


def _interpret_chronos(scored: dict) -> list:
    """Chronos AI予測を定性的に解釈"""
    chr_d = scored.get('chronos_detail', {})
    insights = []

    pred_pct = chr_d.get('predicted_change_pct')
    conf = chr_d.get('confidence')
    if pred_pct is not None:
        pct = float(pred_pct)
        conf_label = ""
        if conf is not None:
            conf_val = float(conf)
            conf_label = "高確信" if conf_val > 0.7 else "中確信" if conf_val > 0.4 else "低確信"
        else:
            conf_label = "確信度不明"

        if abs(pct) < 0.1:
            insights.append(f"AI予測: ほぼ横ばい（{pct:+.2f}%、{conf_label}）")
        elif pct > 1.0:
            insights.append(f"AI予測: {pct:+.2f}%の大幅上昇を予想（{conf_label}）")
        elif pct > 0:
            insights.append(f"AI予測: {pct:+.2f}%の上昇を予想（{conf_label}）")
        elif pct < -1.0:
            insights.append(f"AI予測: {pct:+.2f}%の大幅下落を予想（{conf_label}）")
        else:
            insights.append(f"AI予測: {pct:+.2f}%の下落を予想（{conf_label}）")

        # 予測レンジ（不確実性）
        q10 = chr_d.get('q10_change_pct')
        q90 = chr_d.get('q90_change_pct')
        if q10 is not None and q90 is not None:
            spread = float(q90) - float(q10)
            if spread > 5:
                insights.append(f"予測レンジ幅{spread:.1f}%と広く不確実性が高い")
            elif spread < 1:
                insights.append(f"予測レンジ幅{spread:.1f}%と狭く方向感に確信")

    return insights


def _interpret_market_context(scored: dict) -> list:
    """市場環境データを定性的に解釈"""
    mkt = scored.get('market_context_detail', {})
    insights = []

    fng = mkt.get('fng_value')
    if fng is not None:
        fng_val = int(fng)
        if fng_val <= 20:
            insights.append(f"Fear & Greed={fng_val}: 極度の恐怖（逆張り買いの好機の可能性）")
        elif fng_val <= 35:
            insights.append(f"Fear & Greed={fng_val}: 恐怖優勢")
        elif fng_val >= 80:
            insights.append(f"Fear & Greed={fng_val}: 極度の貪欲（天井警戒）")
        elif fng_val >= 65:
            insights.append(f"Fear & Greed={fng_val}: 楽観ムード")
        else:
            insights.append(f"Fear & Greed={fng_val}: 中立")

    btc_dom = mkt.get('btc_dominance')
    if btc_dom is not None:
        insights.append(f"BTC Dominance={float(btc_dom):.1f}%")

    return insights


def _interpret_multi_tf(scored: dict) -> list:
    """マルチタイムフレームの方向性を定性的に解釈"""
    tf_breakdown = scored.get('tf_breakdown', {})
    insights = []
    if not tf_breakdown:
        return insights

    # 各TFのシグナル集約
    tf_signals = {}
    for tf in ['15m', '1h', '4h', '1d']:
        if tf in tf_breakdown:
            tf_signals[tf] = tf_breakdown[tf].get('signal', 'HOLD')

    buy_tfs = [tf for tf, s in tf_signals.items() if s == 'BUY']
    sell_tfs = [tf for tf, s in tf_signals.items() if s == 'SELL']

    if buy_tfs and not sell_tfs:
        insights.append(f"{', '.join(buy_tfs)}が買いシグナル → 全体的に強気")
    elif sell_tfs and not buy_tfs:
        insights.append(f"{', '.join(sell_tfs)}が売りシグナル → 全体的に弱気")
    elif buy_tfs and sell_tfs:
        insights.append(f"買い({', '.join(buy_tfs)})と売り({', '.join(sell_tfs)})で時間軸間に方向乖離")

    # 短期 vs 長期
    short_tfs = {tf: tf_breakdown[tf] for tf in ['15m', '1h'] if tf in tf_breakdown}
    long_tfs = {tf: tf_breakdown[tf] for tf in ['4h', '1d'] if tf in tf_breakdown}
    if short_tfs and long_tfs:
        short_avg = sum(d['score'] for d in short_tfs.values()) / len(short_tfs)
        long_avg = sum(d['score'] for d in long_tfs.values()) / len(long_tfs)
        if short_avg > 0.01 and long_avg < -0.01:
            insights.append("短期はリバウンド局面だが上位トレンドは下向き → 戻り売りに注意")
        elif short_avg < -0.01 and long_avg > 0.01:
            insights.append("短期の下押しは一時的で長期トレンドは上向き → 押し目買いの好機か")
        elif short_avg > 0.01 and long_avg > 0.01:
            insights.append("短期・長期ともに強気で一致 → トレンドの信頼度が高い")
        elif short_avg < -0.01 and long_avg < -0.01:
            insights.append("短期・長期ともに弱気で一致 → 下落トレンドの信頼度が高い")
        else:
            insights.append("短期・長期ともに方向感が弱く中立")

    return insights


def generate_ai_comment(scored: dict, thresholds: dict) -> str:
    """Bedrock (Claude 3 Haiku) で専門家レベルの分析コメントを生成

    数値スコアは一切含めず、定性的な市場解釈を提供する。
    指標値（RSI, ADX, F&G等）は意味のある文脈でのみ引用。
    """
    try:
        pair = scored.get('pair', 'unknown')
        coin_name = TRADING_PAIRS.get(pair, {}).get('name', pair.upper())
        total = scored.get('total_score', 0)

        # シグナル判定
        signal = 'HOLD'
        if total >= thresholds.get('buy', BASE_BUY_THRESHOLD):
            signal = 'BUY'
        elif total <= thresholds.get('sell', BASE_SELL_THRESHOLD):
            signal = 'SELL'
        signal_jp = {'BUY': '買い', 'SELL': '売り', 'HOLD': '様子見'}[signal]

        # === 各データソースを定性的に解釈（Python側で前処理） ===
        tech_insights = _interpret_indicators(scored)
        ai_insights = _interpret_chronos(scored)
        mkt_insights = _interpret_market_context(scored)
        tf_insights = _interpret_multi_tf(scored)

        # ニュースヘッドライン
        news = scored.get('news_headlines', [])
        news_items = [f"「{n.get('title', '')}」" for n in news[:3] if n.get('title')]

        # === 定性データをプロンプトに構成 ===
        NL = '\n'
        materials = f"""通貨: {coin_name}
シグナル判定: {signal_jp}

【テクニカル分析】
{NL.join('・' + i for i in tech_insights) if tech_insights else '・データ不足'}

【AI予測（Chronos）】
{NL.join('・' + i for i in ai_insights) if ai_insights else '・データ不足'}

【市場環境】
{NL.join('・' + i for i in mkt_insights) if mkt_insights else '・データ不足'}

【マルチタイムフレーム分析】
{NL.join('・' + i for i in tf_insights) if tf_insights else '・データ不足'}"""

        if news_items:
            materials += f"\n\n【最近のニュース】\n{'、'.join(news_items)}"

        prompt = f"""あなたはヘッジファンドの仮想通貨トレーディングデスクのシニアアナリストです。
以下の分析データから市場の「物語」を読み取り、個人投資家向けの分析コメントを日本語で作成してください。

{materials}

【コメント作成ルール】
1. 今の相場状況を一言で特徴づけてから始める（例:「調整局面入りの兆しです」「底固めからの反発初動と見られます」）
2. 最も重要な根拠を1-2点だけ挙げる。数値の羅列ではなく「なぜそれが重要か」を説明すること
3. リスクや注意点を1点挙げる（反対シナリオの可能性）
4. 数値スコアや閾値の数字は絶対に書かない。指標値（RSI, ADX, Fear&Greed）だけ自然に引用してよい
5. です・ます調で3〜5文、450文字以内
6. シグナル判定「{signal_jp}」の根拠を自然に織り込む"""

        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.4},
        )

        comment = response['output']['message']['content'][0]['text'].strip()
        # 改行を除去して1行にする
        comment = comment.replace('\n', ' ').strip()
        # 長すぎる場合は切り詰め
        if len(comment) > 500:
            comment = comment[:497] + '...'

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
            item['news_headlines'] = to_dynamo_map({'h': news_headlines[:5]})['h']

        market_detail = scored.get('market_context_detail', {})
        if market_detail:
            item['market_detail'] = to_dynamo_map(market_detail)

        ai_comment = scored.get('ai_comment', '')
        if ai_comment:
            item['ai_comment'] = ai_comment

        table.put_item(Item=item)
    except Exception as e:
        print(f"Error saving signal for {scored.get('pair', 'unknown')}: {e}")


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
            header_text = f"📊 マルチTF通貨分析: {' / '.join(parts)}"
        else:
            header_text = "⚪ マルチTF通貨分析: ALL HOLD"

        # スコアバー
        def score_bar(score):
            pos = int((score + 1) * 5)
            pos = max(0, min(10, pos))
            return '▓' * pos + '░' * (10 - pos)

        # ランキング表示（通貨別判定付き + マルチTFブレークダウン）
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

            # マルチTFブレークダウン
            tf_breakdown = s.get('tf_breakdown', {})
            alignment = s.get('alignment', 'unknown')
            align_emoji = {'aligned': '✅', 'conflicting': '⚠️', 'mixed': '➖'}.get(alignment, '❓')

            ranking_text += (
                f"{medal} *{name}*: `{s['total_score']:+.4f}` {score_bar(s['total_score'])} → {signal_emoji}\n"
                f"    Tech: `{s['components']['technical']:+.3f}` | "
                f"AI: `{s['components']['chronos']:+.3f}` | "
                f"Sent: `{s['components']['sentiment']:+.3f}` | "
                f"Mkt: `{s['components'].get('market_context', 0):+.3f}`\n"
            )

            # TFブレークダウン表示（per-TF シグナル込み）
            if tf_breakdown:
                tf_parts = []
                for tf in ['15m', '1h', '4h', '1d']:
                    if tf in tf_breakdown:
                        tf_data = tf_breakdown[tf]
                        tf_sig = tf_data.get('signal', 'HOLD')
                        sig_icon = {'BUY': '🟢', 'SELL': '🔴', 'HOLD': '⚪'}.get(tf_sig, '⚪')
                        tf_parts.append(f"{tf}:`{tf_data['score']:+.3f}`{sig_icon}")
                ranking_text += f"    TF: {' | '.join(tf_parts)} {align_emoji}{alignment}\n"

            ranking_text += f"    閾値: BUY≥`{pair_th['buy']:+.3f}` / SELL≤`{pair_th['sell']:+.3f}`\n"

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
                    {"type": "mrkdwn", "text": f"マルチTF: 15m={TIMEFRAME_WEIGHTS.get('15m', 0):.0%} 1h={TIMEFRAME_WEIGHTS.get('1h', 0):.0%} "
                                                f"4h={TIMEFRAME_WEIGHTS.get('4h', 0):.0%} 1d={TIMEFRAME_WEIGHTS.get('1d', 0):.0%} | "
                                                f"基準閾値: BUY≥`{BASE_BUY_THRESHOLD:+.3f}` / SELL≤`{BASE_SELL_THRESHOLD:+.3f}` (ボラ補正あり)"
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
