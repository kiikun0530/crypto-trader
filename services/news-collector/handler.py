"""
ニュース収集 Lambda
30分間隔でCryptoPanicから全通貨のニュースを一括取得し、通貨別センチメント分析

API最適化:
- 全通貨を1リクエストで取得（currencies=ETH,BTC,XRP,...）
- 全体市場ニュースを1リクエストで取得
- 合計2 API calls/実行 × 1,440回/月 = 2,880/mo（Growth Plan 3,000内）

改善点:
- 各通貨ごとにセンチメントスコアを個別保存
- BTC相関・全体市場の影響を加味
- 投票数の信頼性閾値を維持
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

# 通貨ペア設定
DEFAULT_PAIRS = {
    "eth_usdt": {"binance": "ETHUSDT", "coincheck": "eth_jpy", "news": "ETH", "name": "Ethereum"}
}
TRADING_PAIRS = json.loads(os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS)))

NEWS_LIMIT = 50
NEWS_FRESHNESS_HOURS = 1

# 投票信頼性の閾値
MIN_RELIABLE_VOTES = 5
VOTE_CONFIDENCE_CAP = 20

# BTC相関の重み（BTC以外の通貨に適用）
BTC_CORRELATION_WEIGHT = 0.5

# 全体市場ニュースの重み
MARKET_NEWS_WEIGHT = 0.3


def handler(event, context):
    """全通貨のニュース収集 + 通貨別センチメント分析"""
    timestamp = int(time.time())

    try:
        # 1. 対象通貨のニュースを一括取得（1 API call）
        target_currencies = list(set([c['news'] for c in TRADING_PAIRS.values()]))
        currency_news = fetch_news(currencies=','.join(target_currencies), limit=NEWS_LIMIT)
        print(f"Fetched {len(currency_news)} articles for {','.join(target_currencies)}")

        # 2. 全体市場ニュース取得（1 API call）
        market_news = fetch_news(currencies=None, limit=20)
        print(f"Fetched {len(market_news)} market-wide articles")

        # 3. 通貨別にセンチメント計算・保存
        results = {}
        for pair, config in TRADING_PAIRS.items():
            currency = config['news']

            # この通貨に直接関連するニュース
            direct_news = [a for a in currency_news if is_about_currency(a, currency)]

            # BTC相関ニュース（BTC以外の通貨）
            btc_news = []
            if currency != 'BTC':
                btc_news = [a for a in currency_news if is_about_currency(a, 'BTC')]

            # 重み付けして結合
            weighted_articles = []
            seen_ids = set()

            for article in direct_news:
                a = dict(article)
                a['_currency_weight'] = 1.0
                a['_source_currency'] = currency
                weighted_articles.append(a)
                seen_ids.add(article.get('id'))

            for article in btc_news:
                if article.get('id') not in seen_ids:
                    a = dict(article)
                    a['_currency_weight'] = BTC_CORRELATION_WEIGHT
                    a['_source_currency'] = 'BTC'
                    weighted_articles.append(a)
                    seen_ids.add(article.get('id'))

            for article in market_news:
                a = dict(article)
                a['_currency_weight'] = MARKET_NEWS_WEIGHT
                a['_source_currency'] = 'ALL'
                weighted_articles.append(a)

            # センチメント分析
            score, fresh_count, stats = analyze_sentiment_weighted(weighted_articles)
            save_sentiment(pair, timestamp, score, len(weighted_articles), fresh_count)

            results[pair] = {
                'score': round(score, 3),
                'direct': len(direct_news),
                'btc_context': len(btc_news),
                'market': len(market_news),
                'total': len(weighted_articles)
            }
            print(f"  {config['name']} ({pair}): score={score:.3f} "
                  f"(direct={len(direct_news)}, btc={len(btc_news)}, market={len(market_news)})")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'pairs_analyzed': len(results),
                'results': results,
                'api_calls': 2,
                'timestamp': timestamp
            })
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def is_about_currency(article: dict, currency: str) -> bool:
    """記事が特定の通貨に関連するかチェック"""
    # CryptoPanic API v2 では 'instruments' フィールドに通貨情報がある
    # v1 の 'currencies' フィールドもフォールバックで対応
    for field in ['instruments', 'currencies']:
        items = article.get(field, [])
        if isinstance(items, list):
            for c in items:
                if isinstance(c, dict) and c.get('code', '').upper() == currency.upper():
                    return True
                elif isinstance(c, str) and c.upper() == currency.upper():
                    return True
    return False


def fetch_news(currencies: str = None, limit: int = 50) -> list:
    """CryptoPanic APIからニュース取得"""
    if not CRYPTOPANIC_API_KEY:
        print("No CryptoPanic API key, using neutral sentiment")
        return []

    base_url = 'https://cryptopanic.com/api/growth/v2/posts/'
    params = f'?auth_token={CRYPTOPANIC_API_KEY}&kind=news&public=true'

    if currencies:
        params += f'&currencies={currencies}'

    try:
        url = base_url + params
        label = currencies or 'ALL'
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')

        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            results = data.get('results', [])[:limit]
            return results
    except Exception as e:
        print(f"Error fetching news ({currencies or 'ALL'}): {str(e)}")
        return []


def analyze_sentiment_weighted(news: list) -> tuple:
    """時間加重センチメント分析（投票信頼性考慮）"""
    if not news:
        return 0.5, 0, {}

    current_time = time.time()
    total_weighted_score = 0
    total_weight = 0
    fresh_count = 0

    vote_reliable_count = 0
    vote_unreliable_count = 0
    sentiment_field_count = 0

    for article in news:
        # 記事の新鮮さ
        published = article.get('published_at', '')
        article_age_hours = get_article_age_hours(published)

        if article_age_hours <= NEWS_FRESHNESS_HOURS:
            fresh_count += 1

        # 時間減衰: 新しいほど重み大（1時間以内=1.0、24時間=0.1）
        time_weight = max(0.1, 1.0 - (article_age_hours / 24))

        # 通貨別重み
        currency_weight = article.get('_currency_weight', 1.0)

        # 投票データ
        votes = article.get('votes', {})
        positive = votes.get('positive', 0) + votes.get('important', 0) * 1.5
        negative = votes.get('negative', 0) + votes.get('toxic', 0) * 1.5
        liked = votes.get('liked', 0)
        disliked = votes.get('disliked', 0)
        total_votes = positive + negative + liked + disliked

        if total_votes >= MIN_RELIABLE_VOTES:
            vote_reliable_count += 1
            article_score = (positive + liked) / total_votes
            vote_confidence = min(
                (total_votes - MIN_RELIABLE_VOTES) / (VOTE_CONFIDENCE_CAP - MIN_RELIABLE_VOTES),
                1.0
            )
            article_score = 0.5 + (article_score - 0.5) * vote_confidence
        else:
            vote_unreliable_count += 1
            # 投票データ不足時はタイトルベースの簡易センチメント
            article_score = estimate_sentiment_from_title(article.get('title', ''))

        # CryptoPanicのsentimentフィールド（v1互換）
        sentiment = article.get('sentiment', '')
        if sentiment:
            sentiment_field_count += 1
            if sentiment == 'bullish':
                article_score = min(article_score + 0.15, 1.0)
            elif sentiment == 'bearish':
                article_score = max(article_score - 0.15, 0.0)

        # panic_score があれば補助的に使用（v2）
        panic_score = article.get('panic_score')
        if panic_score is not None and isinstance(panic_score, (int, float)):
            # panic_score: 0=ネガティブ, 2=中立, 4=ポジティブ
            panic_adjustment = (panic_score - 2) * 0.05  # ±0.10 の微調整
            article_score = max(0.0, min(1.0, article_score + panic_adjustment))

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
    """記事の経過時間（時間単位）"""
    if not published_at:
        return 24

    try:
        from datetime import datetime
        published = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        now = datetime.now(published.tzinfo)
        age_seconds = (now - published).total_seconds()
        return max(0, age_seconds / 3600)
    except:
        return 24


def estimate_sentiment_from_title(title: str) -> float:
    """タイトルからセンチメントを簡易推定（投票データ不足時のフォールバック）"""
    if not title:
        return 0.5

    title_lower = title.lower()

    bullish_keywords = [
        'surge', 'soar', 'rally', 'breakout', 'bullish', 'pump', 'moon',
        'all-time high', 'ath', 'gain', 'rises', 'jumps', 'boost',
        'adoption', 'partnership', 'upgrade', 'approval', 'etf approved',
        'inflow', 'accumulate', 'buying', 'bought', 'record high',
        'outperform', 'momentum', 'recovery', 'rebound', 'reclaim',
    ]
    bearish_keywords = [
        'crash', 'plunge', 'dump', 'bearish', 'sell-off', 'selloff',
        'collapse', 'decline', 'drop', 'falls', 'tank', 'slump',
        'hack', 'exploit', 'vulnerability', 'fraud', 'scam',
        'ban', 'restriction', 'crackdown', 'regulate', 'lawsuit',
        'outflow', 'liquidat', 'fear', 'panic', 'warning', 'risk',
        'fud', 'bubble', 'overvalued', 'correction',
    ]

    bullish_count = sum(1 for kw in bullish_keywords if kw in title_lower)
    bearish_count = sum(1 for kw in bearish_keywords if kw in title_lower)

    if bullish_count == 0 and bearish_count == 0:
        return 0.5

    # スコア調整: キーワード1つにつき ±0.1、最大 ±0.3
    net_score = (bullish_count - bearish_count) * 0.1
    net_score = max(-0.3, min(0.3, net_score))
    return 0.5 + net_score


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
