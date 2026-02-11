"""
センチメント取得 Lambda
DynamoDBから最新のセンチメントスコアを取得
"""
import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
SENTIMENT_TABLE = os.environ.get('SENTIMENT_TABLE', 'eth-trading-sentiment')
ANALYSIS_STATE_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')


def _update_pipeline(stage, status, detail=''):
    try:
        table = dynamodb.Table(ANALYSIS_STATE_TABLE)
        now = int(__import__('time').time())
        table.update_item(
            Key={'pair': 'pipeline_status'},
            UpdateExpression='SET #s = :info, updated_at = :ts',
            ExpressionAttributeNames={'#s': stage},
            ExpressionAttributeValues={
                ':info': {'status': status, 'timestamp': now, 'detail': detail},
                ':ts': now,
            },
        )
    except Exception:
        pass


def handler(event, context):
    """センチメント取得"""
    pair = event.get('pair', 'eth_usdt')
    _update_pipeline('sentiment', 'running', f'{pair} センチメント取得中')
    
    try:
        # 最新のセンチメントスコア取得
        score, timestamp = get_latest_sentiment(pair)
        
        _update_pipeline('sentiment', 'completed', f'{pair} score={score}')
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
