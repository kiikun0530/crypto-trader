"""
アグリゲーター Lambda
全通貨のテクニカル、Chronos、センチメントスコアを統合
最も期待値の高い通貨を特定し、売買シグナルを生成

マルチ通貨ロジック:
- 全通貨をスコアリングしてランキング
- SELL優先: 保有ポジションでSELL閾値以下があれば売り
- BUY: 未保有通貨でBUY閾値超えがあれば買い（複数同時保有OK）
- ボラティリティ適応型閾値（市場状況に応じて動的調整）
- 最低保有時間: BUYから30分はシグナルSELLを無視（SL/TPは有効）
- 通貨分散: 同一通貨の同時保有はMAX_POSITIONS_PER_PAIRまで
"""
import json
import os
import time
import boto3
from decimal import Decimal
import urllib.request

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')

SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
ORDER_QUEUE_URL = os.environ.get('ORDER_QUEUE_URL', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

# 通貨ペア設定
DEFAULT_PAIRS = {
    "eth_usdt": {"binance": "ETHUSDT", "coincheck": "eth_jpy", "news": "ETH", "name": "Ethereum"}
}
TRADING_PAIRS = json.loads(os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS)))

# 重み設定
TECHNICAL_WEIGHT = float(os.environ.get('TECHNICAL_WEIGHT', '0.45'))
CHRONOS_WEIGHT = float(os.environ.get('AI_PREDICTION_WEIGHT', '0.40'))
SENTIMENT_WEIGHT = float(os.environ.get('SENTIMENT_WEIGHT', '0.15'))

# ボラティリティ適応型閾値
# 基準閾値（平均的なボラティリティ時に使用）
BASE_BUY_THRESHOLD = float(os.environ.get('BASE_BUY_THRESHOLD', '0.30'))
BASE_SELL_THRESHOLD = float(os.environ.get('BASE_SELL_THRESHOLD', '-0.20'))
# BB幅の基準値（暗号通貨の典型的なBB幅 ≈ 3%）
BASELINE_BB_WIDTH = float(os.environ.get('BASELINE_BB_WIDTH', '0.03'))
# ボラティリティ補正のクランプ範囲
VOL_CLAMP_MIN = 0.5
VOL_CLAMP_MAX = 2.0

# 最低保有時間（秒）: BUYから一定時間はシグナルSELLを無視（SL/TPは有効）
# BUY→即SELL往復ビンタ防止
MIN_HOLD_SECONDS = int(os.environ.get('MIN_HOLD_SECONDS', '1800'))  # デフォルト30分

# 同一通貨の最大同時保有ポジション数（通貨分散ルール）
MAX_POSITIONS_PER_PAIR = int(os.environ.get('MAX_POSITIONS_PER_PAIR', '1'))


def handler(event, context):
    """全通貨の統合スコア計算 + 最適通貨選定"""

    # Step Functionsから Map → analysis_results 形式で受け取る
    pairs_results = event.get('analysis_results', [])

    # 後方互換: 単一ペアの旧形式
    if not pairs_results and 'pair' in event:
        pairs_results = [event]

    try:
        # 1. 全通貨のスコア計算
        scored_pairs = []
        for result in pairs_results:
            pair = result.get('pair', 'unknown')
            scored = score_pair(pair, result)
            scored_pairs.append(scored)

        # 2. ボラティリティ適応型閾値を計算
        buy_threshold, sell_threshold = calculate_dynamic_thresholds(scored_pairs)

        # 3. シグナル保存（動的閾値を使用）
        for scored in scored_pairs:
            save_signal(scored, buy_threshold, sell_threshold)

        # 4. スコア順にソート（期待値の高い順）
        scored_pairs.sort(key=lambda x: x['total_score'], reverse=True)

        # 5. 現在のポジション確認（複数対応）
        active_positions = find_all_active_positions()

        # 6. 売買判定（動的閾値で判定）
        signal, target_pair, target_score = decide_action(
            scored_pairs, active_positions, buy_threshold, sell_threshold
        )

        has_signal = signal in ['BUY', 'SELL']

        # 7. 注文送信
        if has_signal and ORDER_QUEUE_URL:
            send_order_message(target_pair, signal, target_score, int(time.time()))

        result = {
            'signal': signal,
            'target_pair': target_pair,
            'target_score': round(target_score, 4) if target_score else None,
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
            'buy_threshold': round(buy_threshold, 4),
            'sell_threshold': round(sell_threshold, 4),
            'timestamp': int(time.time())
        }

        # 8. Slack通知（ランキング付き + 動的閾値 + 含み損益表示）
        notify_slack(result, scored_pairs, active_positions, buy_threshold, sell_threshold)

        return result

    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'signal': 'HOLD',
            'has_signal': False,
            'error': str(e)
        }


def score_pair(pair: str, result: dict) -> dict:
    """通貨ペアのスコアを計算"""
    technical_result = result.get('technical', {})
    chronos_result = result.get('chronos', {})
    sentiment_result = result.get('sentiment', {})

    technical_score = extract_score(technical_result, 'technical_score', 0.5)
    chronos_score = extract_score(chronos_result, 'chronos_score', 0.5)
    sentiment_score = extract_score(sentiment_result, 'sentiment_score', 0.5)

    # -1〜1スケールに正規化
    technical_normalized = technical_score  # 既に-1〜1
    chronos_normalized = chronos_score  # 既に-1〜1
    sentiment_normalized = (sentiment_score - 0.5) * 2  # 0〜1 → -1〜1

    # 加重平均
    total_score = (
        technical_normalized * TECHNICAL_WEIGHT +
        chronos_normalized * CHRONOS_WEIGHT +
        sentiment_normalized * SENTIMENT_WEIGHT
    )

    # ボラティリティ情報を抽出（BB幅 = (上限-下限)/中央値）
    bb_width = extract_bb_width(technical_result)

    return {
        'pair': pair,
        'total_score': total_score,
        'components': {
            'technical': round(technical_normalized, 3),
            'chronos': round(chronos_normalized, 3),
            'sentiment': round(sentiment_normalized, 3)
        },
        # ⚠️ この価格はBinance USDT建て（例: ETH ~$2,100）
        # Coincheck JPY建てのポジション価格と比較してはいけない
        # P/L計算にはget_current_price()でJPY価格を別途取得すること
        'current_price_usd': result.get('technical', {}).get('current_price', 0),
        'bb_width': bb_width
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


def calculate_dynamic_thresholds(scored_pairs: list) -> tuple:
    """
    ボラティリティ適応型閾値を計算

    ロジック:
    - 全通貨の平均BB幅（ボラティリティ指標）を算出
    - 基準BB幅(3%)と比較して補正係数を計算
    - 高ボラ時: 閾値を厳しく（ノイズに反応しない）
    - 低ボラ時: 閾値を緩く（小さな確実なシグナルを拾う）
    """
    if not scored_pairs:
        return BASE_BUY_THRESHOLD, BASE_SELL_THRESHOLD

    bb_widths = [s.get('bb_width', BASELINE_BB_WIDTH) for s in scored_pairs]
    avg_bb_width = sum(bb_widths) / len(bb_widths)

    vol_ratio = avg_bb_width / BASELINE_BB_WIDTH
    vol_ratio = max(VOL_CLAMP_MIN, min(VOL_CLAMP_MAX, vol_ratio))

    buy_threshold = BASE_BUY_THRESHOLD * vol_ratio
    sell_threshold = BASE_SELL_THRESHOLD * vol_ratio

    print(f"Dynamic thresholds: BUY={buy_threshold:+.3f} SELL={sell_threshold:+.3f} "
          f"(avg_bb_width={avg_bb_width:.4f}, vol_ratio={vol_ratio:.2f})")

    return buy_threshold, sell_threshold


def decide_action(scored_pairs: list, active_positions: list,
                   buy_threshold: float, sell_threshold: float) -> tuple:
    """
    全通貨のスコアから最適なアクションを決定（動的閾値対応・複数ポジション対応）

    ルール:
    1. SELL判定: 保有中ポジションでSELL閾値以下のものがあれば売り（最悪スコア優先）
    2. BUY判定: 未保有の通貨でBUY閾値以上のものがあれば買い（最高スコア優先）
    3. それ以外 → HOLD

    複数ポジション同時保有可。SELLがBUYより優先される。

    Returns: (signal, target_pair, target_score)
    """
    if not scored_pairs:
        return 'HOLD', None, None

    # 保有中のペアをセット化（BUY判定で使用）
    held_coincheck_pairs = set()
    if active_positions:
        held_coincheck_pairs = {p['pair'] for p in active_positions}

    # --- SELL判定（優先） ---
    # ⚠️ 最低保有時間ルール: BUYからMIN_HOLD_SECONDS以内のポジションは
    #    シグナルSELLを無視（SL/TPはposition-monitorが別途処理するため安全）
    now = int(time.time())
    if active_positions:
        sell_candidates = []
        hold_skipped = []
        for position in active_positions:
            position_pair = position['pair']
            entry_time = int(position.get('entry_time', 0))
            hold_elapsed = now - entry_time if entry_time else 999999

            analysis_pair = None
            for pair, config in TRADING_PAIRS.items():
                if config['coincheck'] == position_pair:
                    analysis_pair = pair
                    break

            if analysis_pair:
                pair_data = next((s for s in scored_pairs if s['pair'] == analysis_pair), None)
                if pair_data and pair_data['total_score'] <= sell_threshold:
                    if hold_elapsed < MIN_HOLD_SECONDS:
                        remaining = MIN_HOLD_SECONDS - hold_elapsed
                        hold_skipped.append((position_pair, pair_data['total_score'], remaining))
                        print(f"SELL skipped for {position_pair}: score={pair_data['total_score']:.4f} "
                              f"but hold period active (elapsed={hold_elapsed}s, "
                              f"remaining={remaining}s / {remaining/60:.0f}min)")
                    else:
                        sell_candidates.append((position_pair, pair_data['total_score']))

        if sell_candidates:
            sell_candidates.sort(key=lambda x: x[1])
            target_pair, target_score = sell_candidates[0]
            print(f"SELL signal for {target_pair}: score={target_score:.4f} "
                  f"(threshold: {sell_threshold:.3f})")
            return 'SELL', target_pair, target_score

        if hold_skipped:
            pairs_text = ', '.join(f"{p}(残{r//60}分)" for p, _, r in hold_skipped)
            print(f"SELL suppressed by hold period: {pairs_text}")

    # --- BUY判定（未保有の通貨から最高スコアを選定） ---
    # 通貨分散ルール: 同一通貨はMAX_POSITIONS_PER_PAIRまで
    from collections import Counter
    held_pair_counts = Counter(p['pair'] for p in active_positions) if active_positions else Counter()

    for candidate in scored_pairs:
        coincheck_pair = TRADING_PAIRS.get(candidate['pair'], {}).get('coincheck', candidate['pair'])
        current_count = held_pair_counts.get(coincheck_pair, 0)
        if current_count >= MAX_POSITIONS_PER_PAIR:
            continue  # 同一通貨の保有上限に達している
        if candidate['total_score'] >= buy_threshold:
            print(f"BUY signal for {candidate['pair']} ({coincheck_pair}): "
                  f"score={candidate['total_score']:.4f} (threshold: {buy_threshold:.3f})")
            return 'BUY', coincheck_pair, candidate['total_score']
        else:
            break  # スコア降順なので、閾値未満なら以降も未満

    held_text = ', '.join(held_coincheck_pairs) if held_coincheck_pairs else 'none'
    print(f"HOLD: no actionable signals (held: {held_text})")
    return 'HOLD', None, None


def find_all_active_positions() -> list:
    """全通貨のアクティブポジションを全て検索"""
    table = dynamodb.Table(POSITIONS_TABLE)
    positions = []

    for pair, config in TRADING_PAIRS.items():
        coincheck_pair = config['coincheck']
        try:
            response = table.query(
                KeyConditionExpression='pair = :pair',
                ExpressionAttributeValues={':pair': coincheck_pair},
                ScanIndexForward=False,
                Limit=1
            )
            items = response.get('Items', [])
            if items and not items[0].get('closed'):
                positions.append(items[0])
        except Exception as e:
            print(f"Error checking position for {coincheck_pair}: {e}")

    return positions


def get_current_price(pair: str) -> float:
    """
    Coincheck ticker APIから現在価格を取得（JPY建て）

    ⚠️ score_pair()のcurrent_price_usdはBinance USDT建て。
    ポジションP/L計算には必ずこの関数でJPY価格を取得すること。
    """
    url = f"https://coincheck.com/api/ticker?pair={pair}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        return float(data['last'])


def extract_score(result: dict, key: str, default: float) -> float:
    """結果からスコアを抽出"""
    if isinstance(result, dict):
        if 'body' in result:
            try:
                body = json.loads(result['body']) if isinstance(result['body'], str) else result['body']
                return float(body.get(key, default))
            except:
                pass
        return float(result.get(key, default))
    return default


def save_signal(scored: dict, buy_threshold: float, sell_threshold: float):
    """全通貨のシグナルを保存（分析履歴・動的閾値対応）"""
    table = dynamodb.Table(SIGNALS_TABLE)
    timestamp = int(time.time())

    signal = 'HOLD'
    if scored['total_score'] >= buy_threshold:
        signal = 'BUY'
    elif scored['total_score'] <= sell_threshold:
        signal = 'SELL'

    table.put_item(Item={
        'pair': scored['pair'],
        'timestamp': timestamp,
        'score': Decimal(str(round(scored['total_score'], 4))),
        'signal': signal,
        'technical_score': Decimal(str(round(scored['components']['technical'], 4))),
        'chronos_score': Decimal(str(round(scored['components']['chronos'], 4))),
        'sentiment_score': Decimal(str(round(scored['components']['sentiment'], 4))),
        'buy_threshold': Decimal(str(round(buy_threshold, 4))),
        'sell_threshold': Decimal(str(round(sell_threshold, 4))),
        'bb_width': Decimal(str(round(scored.get('bb_width', BASELINE_BB_WIDTH), 6))),
        'ttl': timestamp + 7776000  # 90日後に削除
    })


def send_order_message(pair: str, signal: str, score: float, timestamp: int):
    """SQSに注文メッセージ送信"""
    sqs.send_message(
        QueueUrl=ORDER_QUEUE_URL,
        MessageBody=json.dumps({
            'pair': pair,
            'signal': signal,
            'score': score,
            'timestamp': timestamp
        })
    )


def notify_slack(result: dict, scored_pairs: list, active_positions: list,
                 buy_threshold: float = None, sell_threshold: float = None):
    """Slackに分析結果を通知（通貨ランキング + 複数ポジションP/L表示）"""
    buy_threshold = buy_threshold or BASE_BUY_THRESHOLD
    sell_threshold = sell_threshold or BASE_SELL_THRESHOLD
    if not SLACK_WEBHOOK_URL:
        return

    try:
        signal = result.get('signal', 'HOLD')
        target_pair = result.get('target_pair', '-')

        emoji_map = {'BUY': '🟢', 'SELL': '🔴', 'HOLD': '⚪'}
        emoji = emoji_map.get(signal, '❓')

        # スコアバー
        def score_bar(score):
            pos = int((score + 1) * 5)
            pos = max(0, min(10, pos))
            return '▓' * pos + '░' * (10 - pos)

        # ランキング表示
        ranking_text = ""
        for i, s in enumerate(scored_pairs):
            name = TRADING_PAIRS.get(s['pair'], {}).get('name', s['pair'])
            medal = ['🥇', '🥈', '🥉'][i] if i < 3 else f'{i+1}.'
            ranking_text += (
                f"{medal} *{name}*: `{s['total_score']:+.4f}` {score_bar(s['total_score'])}\n"
                f"    Tech: `{s['components']['technical']:+.3f}` | "
                f"AI: `{s['components']['chronos']:+.3f}` | "
                f"Sent: `{s['components']['sentiment']:+.3f}`\n"
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
                # scored_pairsのcurrent_priceはBinance USDT建てなのでP/L計算に使えない
                current_price = 0
                try:
                    current_price = get_current_price(pos_pair)
                except Exception as e:
                    print(f"Failed to get current price for {pos_pair}: {e}")

                # 保有時間と最低保有期間ステータス
                entry_time = int(pos.get('entry_time', 0))
                hold_elapsed = int(time.time()) - entry_time if entry_time else 0
                hold_min = hold_elapsed // 60
                if hold_elapsed < MIN_HOLD_SECONDS:
                    remaining_min = (MIN_HOLD_SECONDS - hold_elapsed) // 60
                    hold_status = f" | 🔒 保有{hold_min}分 (あと{remaining_min}分)"
                else:
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

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} マルチ通貨分析: {signal}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*判定*\n{signal}"},
                    {"type": "mrkdwn", "text": f"*対象*\n{target_pair or '-'}"}
                ]
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
                    {"type": "mrkdwn", "text": f"BUY閾値: `{buy_threshold:+.3f}` / SELL閾値: `{sell_threshold:+.3f}`"}
                ]
            }
        ]

        if signal in ['BUY', 'SELL']:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚡ *{signal}注文をキューに送信しました* ({target_pair})"
                }
            })

        message = {"blocks": blocks}

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(message).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=5)
        print(f"Slack notification sent (status: {response.status})")

    except Exception as e:
        print(f"Slack notification failed: {e}")
        import traceback
        traceback.print_exc()
