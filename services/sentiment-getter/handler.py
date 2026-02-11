"""
センチメント取得 Lambda
DynamoDBから最新のセンチメントスコアを取得
"""
import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
SENTIMENT_TABLE = os.environ.get('SENTIMENT_TABLE', 'eth-trading-sentiment')


def handler(event, context):
    """センチメント取得"""
    pair = event.get('pair', 'eth_usdt')
    
    try:
        # 最新のセンチメントスコア取得
        score, timestamp, top_headlines = get_latest_sentiment(pair)
        
        return {
            'pair': pair,
            'sentiment_score': score,
            'last_updated': timestamp,
            'source': 'cryptopanic',
            'top_headlines': top_headlines
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'pair': pair,
            'sentiment_score': 0.5,
            'top_headlines': [],
            'error': str(e)
        }

def get_latest_sentiment(pair: str) -> tuple:
    """最新センチメント取得（top_headlines含む）"""
    table = dynamodb.Table(SENTIMENT_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=1
    )
    
    items = response.get('Items', [])
    if items:
        item = items[0]
        headlines = item.get('top_headlines', [])
        # DynamoDB Decimal → float 変換
        parsed_headlines = []
        for h in headlines:
            parsed_headlines.append({
                'title': h.get('title', ''),
                'score': float(h.get('score', 0.5)),
                'source': h.get('source', ''),
            })
        return float(item.get('score', 0.5)), int(item.get('timestamp', 0)), parsed_headlines
    
    # デフォルト値（中立）
    return 0.5, 0, []
