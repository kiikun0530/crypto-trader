"""
アグリゲーター Lambda
全通貨のテクニカル、Chronos、センチメントスコアを統合
最も期待値の高い通貨を特定し、売買シグナルを生成

マルチ通貨ロジック:
- 全通貨をスコアリングしてランキング
- ポジションなし → 最高スコアの通貨がBUY閾値超えで買い
- ポジションあり → その通貨がSELL閾値以下で売り
- 1ポジション制約（リスク管理）
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

# 閾値
BUY_THRESHOLD = float(os.environ.get('BUY_THRESHOLD', '0.5'))
SELL_THRESHOLD = float(os.environ.get('SELL_THRESHOLD', '-0.5'))


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
            save_signal(scored)

        # 2. スコア順にソート（期待値の高い順）
        scored_pairs.sort(key=lambda x: x['total_score'], reverse=True)

        # 3. 現在のポジション確認
        active_position = find_active_position()

        # 4. 売買判定（全通貨比較）
        signal, target_pair, target_score = decide_action(scored_pairs, active_position)

        has_signal = signal in ['BUY', 'SELL']

        # 5. 注文送信
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
            'active_position': active_position.get('pair') if active_position else None,
            'timestamp': int(time.time())
        }

        # 6. Slack通知（ランキング付き）
        notify_slack(result, scored_pairs, active_position)

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

    return {
        'pair': pair,
        'total_score': total_score,
        'components': {
            'technical': round(technical_normalized, 3),
            'chronos': round(chronos_normalized, 3),
            'sentiment': round(sentiment_normalized, 3)
        },
        'current_price': result.get('technical', {}).get('current_price', 0)
    }


def decide_action(scored_pairs: list, active_position: dict) -> tuple:
    """
    全通貨のスコアから最適なアクションを決定

    ルール:
    1. ポジションなし → 最高スコアの通貨がBUY閾値以上なら買い
    2. ポジションあり → その通貨がSELL閾値以下なら売り
    3. それ以外 → HOLD

    Returns: (signal, target_pair, target_score)
    """
    if not scored_pairs:
        return 'HOLD', None, None

    if active_position:
        # ポジションあり → 現在の通貨のスコアをチェック
        position_pair = active_position['pair']  # Coincheck pair (e.g., eth_jpy)

        # Coincheck pair → analysis pair の逆引き
        analysis_pair = None
        for pair, config in TRADING_PAIRS.items():
            if config['coincheck'] == position_pair:
                analysis_pair = pair
                break

        if analysis_pair:
            pair_data = next((s for s in scored_pairs if s['pair'] == analysis_pair), None)
            if pair_data and pair_data['total_score'] <= SELL_THRESHOLD:
                print(f"SELL signal for {position_pair}: score={pair_data['total_score']:.4f}")
                return 'SELL', position_pair, pair_data['total_score']

        print(f"HOLD: active position in {position_pair}")
        return 'HOLD', None, None

    else:
        # ポジションなし → 最高スコアの通貨をチェック
        best = scored_pairs[0]
        if best['total_score'] >= BUY_THRESHOLD:
            coincheck_pair = TRADING_PAIRS.get(best['pair'], {}).get('coincheck', best['pair'])
            print(f"BUY signal for {best['pair']} ({coincheck_pair}): score={best['total_score']:.4f}")
            return 'BUY', coincheck_pair, best['total_score']

        print(f"HOLD: best score is {best['total_score']:.4f} (threshold: {BUY_THRESHOLD})")
        return 'HOLD', None, None


def find_active_position() -> dict:
    """全通貨のアクティブポジションを検索"""
    table = dynamodb.Table(POSITIONS_TABLE)

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
                return items[0]
        except Exception as e:
            print(f"Error checking position for {coincheck_pair}: {e}")

    return {}


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


def save_signal(scored: dict):
    """全通貨のシグナルを保存（分析履歴）"""
    table = dynamodb.Table(SIGNALS_TABLE)
    timestamp = int(time.time())

    signal = 'HOLD'
    if scored['total_score'] >= BUY_THRESHOLD:
        signal = 'BUY'
    elif scored['total_score'] <= SELL_THRESHOLD:
        signal = 'SELL'

    table.put_item(Item={
        'pair': scored['pair'],
        'timestamp': timestamp,
        'score': Decimal(str(round(scored['total_score'], 4))),
        'signal': signal,
        'technical_score': Decimal(str(round(scored['components']['technical'], 4))),
        'chronos_score': Decimal(str(round(scored['components']['chronos'], 4))),
        'sentiment_score': Decimal(str(round(scored['components']['sentiment'], 4))),
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


def notify_slack(result: dict, scored_pairs: list, active_position: dict):
    """Slackに分析結果を通知（通貨ランキング表示）"""
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

        # ポジション情報
        position_text = "なし"
        if active_position:
            pos_pair = active_position.get('pair', '?')
            entry_price = float(active_position.get('entry_price', 0))
            position_text = f"{pos_pair} (参入: ¥{entry_price:,.0f})"

        message = {
            "blocks": [
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
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"📍 ポジション: {position_text} | BUY閾値: {BUY_THRESHOLD} / SELL閾値: {SELL_THRESHOLD}"}
                    ]
                }
            ]
        }

        if signal in ['BUY', 'SELL']:
            message["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚡ *{signal}注文をキューに送信しました* ({target_pair})"
                }
            })

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(message).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)

    except Exception as e:
        print(f"Slack notification failed: {e}")
