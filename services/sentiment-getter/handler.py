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
        score, timestamp = get_latest_sentiment(pair)
        
        return {
            'pair': pair,
            'sentiment_score': score,
            'last_updated': timestamp,
            'source': 'cryptopanic'
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'pair': pair,
            'sentiment_score': 0.5,
            'error': str(e)
        }

def get_latest_sentiment(pair: str) -> tuple:
    """最新センチメント取得"""
    table = dynamodb.Table(SENTIMENT_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=1
    )
    
    items = response.get('Items', [])
    if items:
        return float(items[0].get('score', 0.5)), int(items[0].get('timestamp', 0))
    
    # デフォルト値（中立）
    return 0.5, 0
