"""
ニュース収集 Lambda
30分間隔でCryptoPanicからリアルタイムニュースを取得し、センチメント分析
Growth Plan: 3,000 req/mo, Real-time, 1 month history

改善点:
- ETH単独ではなくBTC・全体ニュースも取得（相関性を考慮）
- 投票数の信頼性閾値を導入（少数投票を過信しない）
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

# マルチ通貨ニュース: ETHだけでなくBTC・全体の影響も考慮
# BTC暴落→ETH連動、SEC規制→全体に影響 等
CURRENCY_WEIGHTS = {
    'ETH': 1.0,    # ETH直接ニュースは最重要
    'BTC': 0.6,    # BTC相関（相関係数0.8+だが直接影響は割引）
    'ALL': 0.3,    # 全体市場ニュース（規制、マクロ等）
}

# 投票信頼性の閾値
# CryptoPanicの投票数は記事により0〜数百と分散が大きい
# 少数投票（1-2票）では統計的に無意味なためsentimentフィールドを優先
MIN_RELIABLE_VOTES = 5   # この数以上で投票結果を信頼
VOTE_CONFIDENCE_CAP = 20 # この数以上で信頼度100%

def handler(event, context):
    """ニュース収集 + センチメント分析"""
    pair = 'eth_usdt'  # Binance統一
    
    try:
        # 1. マルチ通貨ニュース取得（ETH + BTC + 全体）
        all_news = []
        for currency, weight in CURRENCY_WEIGHTS.items():
            if currency == 'ALL':
                # 全体ニュース（通貨指定なし）
                news = fetch_news(currency=None, limit=20)
            else:
                news = fetch_news(currency=currency, limit=NEWS_LIMIT)
            
            # 通貨重みを各記事に付与
            for article in news:
                article['_currency_weight'] = weight
                article['_source_currency'] = currency
            all_news.extend(news)
            print(f"  {currency}: {len(news)} articles (weight: {weight})")
        
        if not all_news:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'No news found',
                    'sentiment_score': 0.5
                })
            }
        
        # 2. 時間加重センチメント分析（投票信頼性考慮）
        sentiment_score, fresh_count, stats = analyze_sentiment_weighted(all_news)
        
        # 3. DynamoDBに保存
        timestamp = int(time.time())
        save_sentiment(pair, timestamp, sentiment_score, len(all_news), fresh_count)
        
        print(f"Sentiment: {sentiment_score:.3f} | "
              f"News: {len(all_news)} (fresh: {fresh_count}) | "
              f"Vote stats: {stats}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'pair': pair,
                'news_count': len(all_news),
                'fresh_news_count': fresh_count,
                'sentiment_score': round(sentiment_score, 3),
                'breakdown': stats,
                'timestamp': timestamp
            })
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

def fetch_news(currency: str = None, limit: int = 50) -> list:
    """CryptoPanic APIからリアルタイムニュース取得（Growth Plan対応）"""
    base_url = 'https://cryptopanic.com/api/growth/v2/posts/'
    params = f'?auth_token={CRYPTOPANIC_API_KEY}&kind=news&public=true'
    
    if currency:
        params += f'&currencies={currency}'
    
    if not CRYPTOPANIC_API_KEY:
        print("No CryptoPanic API key, using neutral sentiment")
        return []
    
    try:
        url = base_url + params
        label = currency or 'ALL'
        print(f"Fetching {label} news from CryptoPanic")
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            results = data.get('results', [])[:limit]
            print(f"Fetched {len(results)} {label} articles")
            return results
    except Exception as e:
        print(f"Error fetching {currency or 'ALL'} news: {str(e)}")
        return []

def analyze_sentiment_weighted(news: list) -> tuple:
    """
    時間加重センチメント分析（投票信頼性考慮）
    
    改善点:
    - 通貨別の重み付け（ETH > BTC > 全体）
    - 投票数の信頼性閾値を導入
      - MIN_RELIABLE_VOTES未満: sentimentフィールドとタイトルに依存
      - MIN_RELIABLE_VOTES以上: 投票結果を信頼して活用
    - 投票数が少ない場合は中立寄りに補正
    """
    if not news:
        return 0.5, 0, {}
    
    current_time = time.time()
    total_weighted_score = 0
    total_weight = 0
    fresh_count = 0
    
    # 統計情報
    vote_reliable_count = 0   # 投票が信頼できる記事数
    vote_unreliable_count = 0 # 投票が不十分な記事数
    sentiment_field_count = 0 # sentimentフィールドがある記事数
    
    for article in news:
        # 記事の新鮮さを計算
        published = article.get('published_at', '')
        article_age_hours = get_article_age_hours(published)
        
        if article_age_hours <= NEWS_FRESHNESS_HOURS:
            fresh_count += 1
        
        # 時間減衰: 新しいほど重み大（1時間以内=1.0、24時間=0.1）
        time_weight = max(0.1, 1.0 - (article_age_hours / 24))
        
        # 通貨別重み（ETH直接 > BTC > 全体）
        currency_weight = article.get('_currency_weight', 1.0)
        
        # CryptoPanicの投票データ
        votes = article.get('votes', {})
        positive = votes.get('positive', 0) + votes.get('important', 0) * 1.5
        negative = votes.get('negative', 0) + votes.get('toxic', 0) * 1.5
        liked = votes.get('liked', 0)
        disliked = votes.get('disliked', 0)
        total_votes = positive + negative + liked + disliked
        
        # === 投票信頼性の判定 ===
        if total_votes >= MIN_RELIABLE_VOTES:
            # 十分な投票数: 投票結果を信頼
            vote_reliable_count += 1
            article_score = (positive + liked) / total_votes
            
            # 信頼度: MIN_RELIABLE_VOTES〜VOTE_CONFIDENCE_CAPで線形に上昇
            vote_confidence = min(
                (total_votes - MIN_RELIABLE_VOTES) / (VOTE_CONFIDENCE_CAP - MIN_RELIABLE_VOTES),
                1.0
            )
            # 投票スコアと中立(0.5)のブレンド
            # 信頼度が低いほど中立寄り
            article_score = 0.5 + (article_score - 0.5) * vote_confidence
        else:
            # 投票不十分: sentimentフィールドがあればそれを使う、なければ中立
            vote_unreliable_count += 1
            article_score = 0.5  # 基本は中立
        
        # CryptoPanicのsentimentフィールド（APIが提供する場合）
        # 投票数に関わらず参考にできる（CryptoPanic独自のアルゴリズム）
        sentiment = article.get('sentiment', '')
        if sentiment:
            sentiment_field_count += 1
            if sentiment == 'bullish':
                article_score = min(article_score + 0.15, 1.0)
            elif sentiment == 'bearish':
                article_score = max(article_score - 0.15, 0.0)
        
        # 総合重み = 時間 × 通貨関連性
        weight = time_weight * currency_weight
        total_weighted_score += article_score * weight
        total_weight += weight
    
    if total_weight == 0:
        return 0.5, fresh_count, {}
    
    final_score = total_weighted_score / total_weight
    
    stats = {
        'total_articles': len(news),
        'vote_reliable': vote_reliable_count,
        'vote_unreliable': vote_unreliable_count,
        'has_sentiment_field': sentiment_field_count
    }
    
    return final_score, fresh_count, stats

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
