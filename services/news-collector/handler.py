"""
ニュース収集 Lambda
30分間隔でCryptoPanicからリアルタイムニュースを取得し、センチメント分析
Growth Plan: 3,000 req/mo, Real-time, 1 month history
"""
import json
import os
import time
import urllib.request
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
SENTIMENT_TABLE = os.environ.get('SENTIMENT_TABLE', 'eth-trading-sentiment')
CRYPTOPANIC_API_KEY = os.environ.get('CRYPTOPANIC_API_KEY', '')

# Growth Plan: 50件/リクエスト、1時間以内のニュースをフィルタ
NEWS_LIMIT = 50
NEWS_FRESHNESS_HOURS = 1  # 直近1時間のニュースを重視

def handler(event, context):
    """ニュース収集 + センチメント分析"""
    pair = 'eth_usdt'  # Binance統一
    currency = 'ETH'
    
    try:
        # 1. CryptoPanicからリアルタイムニュース取得
        news = fetch_news(currency, limit=NEWS_LIMIT)
        
        if not news:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'No news found',
                    'sentiment_score': 0.5
                })
            }
        
        # 2. 時間加重センチメント分析
        sentiment_score, fresh_count = analyze_sentiment_weighted(news)
        
        # 3. DynamoDBに保存
        timestamp = int(time.time())
        save_sentiment(pair, timestamp, sentiment_score, len(news), fresh_count)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'pair': pair,
                'news_count': len(news),
                'fresh_news_count': fresh_count,
                'sentiment_score': round(sentiment_score, 3),
                'timestamp': timestamp
            })
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

def fetch_news(currency: str, limit: int = 50) -> list:
    """CryptoPanic APIからリアルタイムニュース取得（Growth Plan対応）"""
    # API v2 + Growth Plan endpoint
    base_url = 'https://cryptopanic.com/api/growth/v2/posts/'
    params = f'?auth_token={CRYPTOPANIC_API_KEY}&currencies={currency}&kind=news&public=true'
    
    if not CRYPTOPANIC_API_KEY:
        print("No CryptoPanic API key, using neutral sentiment")
        return []
    
    try:
        url = base_url + params
        print(f"Fetching news from: {base_url}?currencies={currency}&kind=news")
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            results = data.get('results', [])[:limit]
            print(f"Fetched {len(results)} news articles")
            return results
    except Exception as e:
        print(f"Error fetching news: {str(e)}")
        return []

def analyze_sentiment_weighted(news: list) -> tuple:
    """
    時間加重センチメント分析
    - 新しいニュースほど重み大
    - 投票数も考慮
    - bullish/bearish フィルタを活用
    """
    if not news:
        return 0.5, 0
    
    current_time = time.time()
    total_weighted_score = 0
    total_weight = 0
    fresh_count = 0
    
    for article in news:
        # 記事の新鮮さを計算
        published = article.get('published_at', '')
        article_age_hours = get_article_age_hours(published)
        
        if article_age_hours <= NEWS_FRESHNESS_HOURS:
            fresh_count += 1
        
        # 時間減衰: 新しいほど重み大（1時間以内=1.0、24時間=0.1）
        time_weight = max(0.1, 1.0 - (article_age_hours / 24))
        
        # CryptoPanicの投票データ
        votes = article.get('votes', {})
        positive = votes.get('positive', 0) + votes.get('important', 0) * 1.5
        negative = votes.get('negative', 0) + votes.get('toxic', 0) * 1.5
        liked = votes.get('liked', 0)
        disliked = votes.get('disliked', 0)
        
        # 記事スコア計算
        vote_count = positive + negative + liked + disliked
        if vote_count > 0:
            article_score = (positive + liked) / vote_count
            # 投票数が多いほど信頼性が高い
            vote_weight = min(vote_count / 50, 1.0)
        else:
            # 投票がない場合は中立
            article_score = 0.5
            vote_weight = 0.1
        
        # CryptoPanicのsentimentフィールド（あれば）
        sentiment = article.get('sentiment', '')
        if sentiment == 'bullish':
            article_score = min(article_score + 0.2, 1.0)
        elif sentiment == 'bearish':
            article_score = max(article_score - 0.2, 0.0)
        
        # 総合重み
        weight = time_weight * (0.5 + vote_weight * 0.5)
        total_weighted_score += article_score * weight
        total_weight += weight
    
    if total_weight == 0:
        return 0.5, fresh_count
    
    final_score = total_weighted_score / total_weight
    return final_score, fresh_count

def get_article_age_hours(published_at: str) -> float:
    """記事の経過時間を計算（時間単位）"""
    if not published_at:
        return 24  # 不明な場合は24時間扱い
    
    try:
        # ISO 8601 形式: 2026-02-08T01:30:00Z
        from datetime import datetime
        published = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        now = datetime.now(published.tzinfo)
        age_seconds = (now - published).total_seconds()
        return max(0, age_seconds / 3600)
    except:
        return 24

def save_sentiment(pair: str, timestamp: int, score: float, news_count: int, fresh_count: int):
    """センチメント保存"""
    table = dynamodb.Table(SENTIMENT_TABLE)
    table.put_item(Item={
        'pair': pair,
        'timestamp': timestamp,
        'score': Decimal(str(round(score, 4))),
        'news_count': news_count,
        'fresh_news_count': fresh_count,
        'source': 'cryptopanic',
        'ttl': timestamp + 1209600  # 14日後に削除
    })
