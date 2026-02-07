"""
アグリゲーター Lambda
テクニカル、Chronos、センチメントのスコアを統合してシグナル生成
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
ORDER_QUEUE_URL = os.environ.get('ORDER_QUEUE_URL', '')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

# 重み設定
TECHNICAL_WEIGHT = float(os.environ.get('TECHNICAL_WEIGHT', '0.45'))
CHRONOS_WEIGHT = float(os.environ.get('AI_PREDICTION_WEIGHT', '0.40'))
SENTIMENT_WEIGHT = float(os.environ.get('SENTIMENT_WEIGHT', '0.15'))

# 閾値
BUY_THRESHOLD = float(os.environ.get('BUY_THRESHOLD', '0.5'))
SELL_THRESHOLD = float(os.environ.get('SELL_THRESHOLD', '-0.5'))

def handler(event, context):
    """統合スコア計算 + シグナル生成"""
    pair = event.get('pair', 'eth_usdt')
    
    try:
        # 各コンポーネントからのスコア取得
        technical_result = event.get('technical', {})
        chronos_result = event.get('chronos', {})
        sentiment_result = event.get('sentiment', {})
        
        # スコア抽出（0.5をデフォルト=中立）
        technical_score = extract_score(technical_result, 'technical_score', 0.5)
        chronos_score = extract_score(chronos_result, 'chronos_score', 0.5)
        sentiment_score = extract_score(sentiment_result, 'sentiment_score', 0.5)
        
        # -1〜1スケールに変換（sentiment_scoreは0〜1なので変換）
        technical_normalized = technical_score  # 既に-1〜1
        chronos_normalized = chronos_score  # 既に-1〜1
        sentiment_normalized = (sentiment_score - 0.5) * 2  # 0〜1 → -1〜1
        
        # 加重平均
        total_score = (
            technical_normalized * TECHNICAL_WEIGHT +
            chronos_normalized * CHRONOS_WEIGHT +
            sentiment_normalized * SENTIMENT_WEIGHT
        )
        
        # シグナル判定
        signal = 'HOLD'
        if total_score >= BUY_THRESHOLD:
            signal = 'BUY'
        elif total_score <= SELL_THRESHOLD:
            signal = 'SELL'
        
        timestamp = int(time.time())
        
        # シグナル保存
        save_signal(pair, timestamp, total_score, signal, {
            'technical': technical_normalized,
            'chronos': chronos_normalized,
            'sentiment': sentiment_normalized
        })
        
        result = {
            'pair': pair,
            'timestamp': timestamp,
            'total_score': round(total_score, 4),
            'signal': signal,
            'components': {
                'technical': round(technical_normalized, 3),
                'chronos': round(chronos_normalized, 3),
                'sentiment': round(sentiment_normalized, 3)
            },
            'weights': {
                'technical': TECHNICAL_WEIGHT,
                'chronos': CHRONOS_WEIGHT,
                'sentiment': SENTIMENT_WEIGHT
            }
        }
        
        # シグナル発火時にSQSへ送信
        has_signal = signal in ['BUY', 'SELL']
        if has_signal and ORDER_QUEUE_URL:
            send_order_message(pair, signal, total_score, timestamp)
            result['order_queued'] = True
        
        result['has_signal'] = has_signal
        
        # Slackに分析結果を通知
        notify_slack(result)
        
        return result
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'pair': pair,
            'error': str(e),
            'has_signal': False
        }

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

def save_signal(pair: str, timestamp: int, score: float, signal: str, components: dict):
    """シグナル保存"""
    table = dynamodb.Table(SIGNALS_TABLE)
    table.put_item(Item={
        'pair': pair,
        'timestamp': timestamp,
        'score': Decimal(str(round(score, 4))),
        'signal': signal,
        'technical_score': Decimal(str(round(components['technical'], 4))),
        'chronos_score': Decimal(str(round(components['chronos'], 4))),
        'sentiment_score': Decimal(str(round(components['sentiment'], 4))),
        'ttl': timestamp + 7776000  # 90日後に削除
    })

def send_order_message(pair: str, signal: str, score: float, timestamp: int):
    """SQSに注文メッセージ送信"""
    # 分析はeth_usdt (Binance)、取引はeth_jpy (CoinCheck)
    trading_pair = 'eth_jpy' if pair == 'eth_usdt' else pair
    
    sqs.send_message(
        QueueUrl=ORDER_QUEUE_URL,
        MessageBody=json.dumps({
            'pair': trading_pair,
            'signal': signal,
            'score': score,
            'timestamp': timestamp
        })
    )

def notify_slack(result: dict):
    """Slackに分析結果を通知"""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set, skipping notification")
        return
    
    try:
        signal = result.get('signal', 'UNKNOWN')
        total_score = result.get('total_score', 0)
        components = result.get('components', {})
        
        # シグナルに応じた絵文字
        emoji = {
            'BUY': '🟢',
            'SELL': '🔴',
            'HOLD': '⚪'
        }.get(signal, '❓')
        
        # 閾値情報
        threshold_info = f"BUY閾値: {BUY_THRESHOLD} / SELL閾値: {SELL_THRESHOLD}"
        
        # スコアバー生成（-1〜1を可視化）
        def score_bar(score):
            # -1〜1を0〜10に変換
            pos = int((score + 1) * 5)
            pos = max(0, min(10, pos))
            return '▓' * pos + '░' * (10 - pos)
        
        message = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} ETH分析結果: {signal}",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*総合スコア*\n`{total_score:+.4f}`"},
                        {"type": "mrkdwn", "text": f"*判定*\n{signal}"}
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*スコア内訳*\n"
                                f"テクニカル (40%): `{components.get('technical', 0):+.3f}` {score_bar(components.get('technical', 0))}\n"
                                f"Chronos AI (40%): `{components.get('chronos', 0):+.3f}` {score_bar(components.get('chronos', 0))}\n"
                                f"センチメント (20%): `{components.get('sentiment', 0):+.3f}` {score_bar(components.get('sentiment', 0))}"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"📊 {threshold_info}"}
                    ]
                }
            ]
        }
        
        # BUY/SELL時は強調
        if signal in ['BUY', 'SELL']:
            message["blocks"].append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚡ *注文キューに送信しました*"
                }
            })
        
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(message).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
        print("Slack notification sent")
        
    except Exception as e:
        print(f"Slack notification failed: {e}")
