"""
ニュース収集 Lambda
30分間隔でCryptoPanicから全通貨のニュースを一括取得し、通貨別センチメント分析

API最適化:
- 全通貨を1リクエストで取得（currencies=ETH,BTC,XRP,...）
- 全体市場ニュースを1リクエストで取得
- 合計2 API calls/実行 × 1,440回/月 = 2,880/mo（Growth Plan 3,000内）

センチメント分析:
- 投票数≥ 5: CryptoPanicの投票データを使用
- 投票数 < 5: AWS Bedrock (Amazon Nova Micro) でタイトルベースのセンチメント分析
- Bedrock失敗時: ルールベースNLPにフォールバック
"""
import json
import os
import time
import urllib.request
import boto3
import traceback
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from trading_common import TRADING_PAIRS, SENTIMENT_TABLE, dynamodb

# 日本標準時 (UTC+9)
JST = timezone(timedelta(hours=9))

# Bedrock クライアント (LLMセンチメント分析用)
bedrock = boto3.client('bedrock-runtime')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'apac.amazon.nova-micro-v1:0')

CRYPTOPANIC_API_KEY = os.environ.get('CRYPTOPANIC_API_KEY', '')

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
    print(f"Starting news collection for {len(TRADING_PAIRS)} pairs at {timestamp}")

    try:
        # 1. 対象通貨のニュースを一括取得（1 API call）
        target_currencies = list(set([c['news'] for c in TRADING_PAIRS.values()]))
        print(f"Fetching news for currencies: {','.join(target_currencies)}")
        
        currency_news = fetch_news(currencies=','.join(target_currencies), limit=NEWS_LIMIT)
        print(f"Successfully fetched {len(currency_news)} articles for {','.join(target_currencies)}")

        # 2. 全体市場ニュース取得（1 API call）
        print("Fetching market-wide news...")
        market_news = fetch_news(currencies=None, limit=20)
        print(f"Successfully fetched {len(market_news)} market-wide articles")

        # 3. 投票不足記事のLLMセンチメント分析（バッチ）
        all_articles = list({a.get('id'): a for a in currency_news + market_news}.values())
        llm_scores = analyze_titles_with_llm(all_articles)
        print(f"LLM sentiment: {len(llm_scores)} articles scored")

        # 4. 通貨別にセンチメント計算・保存
        results = {}
        failed_pairs = []
        
        for pair, config in TRADING_PAIRS.items():
            try:
                print(f"Processing {pair} ({config['name']})...")
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
                    if article.get('id') not in seen_ids:
                        a = dict(article)
                        a['_currency_weight'] = MARKET_NEWS_WEIGHT
                        a['_source_currency'] = 'ALL'
                        weighted_articles.append(a)
                        seen_ids.add(article.get('id'))

                # センチメント分析
                print(f"Analyzing sentiment for {pair} with {len(weighted_articles)} articles...")
                score, fresh_count, stats = analyze_sentiment_weighted(weighted_articles, llm_scores)
                top_headlines = extract_top_headlines(weighted_articles, llm_scores, overall_score=score)
                
                print(f"Saving sentiment for {pair}...")
                save_sentiment(pair, timestamp, score, len(weighted_articles), fresh_count, top_headlines)

                results[pair] = {
                    'score': round(score, 3),
                    'direct': len(direct_news),
                    'btc_context': len(btc_news),
                    'market': len(market_news),
                    'total': len(weighted_articles)
                }
                
                print(f"Successfully saved sentiment for {pair}")
                print(f"  {config['name']} ({pair}): score={score:.3f} "
                      f"(direct={len(direct_news)}, btc={len(btc_news)}, market={len(market_news)})")

            except Exception as pair_error:
                print(f"Error processing pair {pair}: {str(pair_error)}")
                print(f"Traceback for {pair}: {traceback.format_exc()}")
                failed_pairs.append(pair)
                continue

        print(f"Completed processing. Successful: {len(results)}, Failed: {len(failed_pairs)}")
        if failed_pairs:
            print(f"Failed pairs: {failed_pairs}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'pairs_analyzed': len(results),
                'pairs_failed': len(failed_pairs),
                'failed_pairs': failed_pairs,
                'results': results,
                'api_calls': 2,
                'timestamp': timestamp
            })
        }

    except Exception as e:
        print(f"Critical error in handler: {str(e)}")
        print(f"Full traceback: {traceback.format_exc()}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e), 'traceback': traceback.format_exc()})
        }


def is_about_currency(article: dict, currency: str) -> bool:
    """記事が特定の通貨に関連するかチェック"""
    try:
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
    except Exception as e:
        print(f"Error checking currency {currency} for article: {str(e)}")
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

    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = base_url + params
            label = currencies or 'ALL'
            print(f"API call attempt {attempt + 1}/{max_retries} for {label}")
            
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')

            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                results = data.get('results', [])[:limit]
                print(f"API call successful for {label}, got {len(results)} articles")
                return results
                
        except Exception as e:
            print(f"Error fetching news ({currencies or 'ALL'}), attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                print(f"Failed to fetch news after {max_retries} attempts")
                return []


def analyze_titles_with_llm(articles: list) -> dict:
    """
    投票不足の記事タイトルをAWS Bedrock (Amazon Nova Micro) でバッチ分析

    投票が十分な記事はスキップし、投票不足の記事のみLLMに送信。
    全記事を1回のAPI呼び出しでまとめて処理（コスト効率化）。

    Returns: {article_id: float(0.0-1.0)} のdict。Bedrock失敗時は空dict
    """
    # 投票不足の記事のみ抽出
    low_vote_articles = []
    for article in articles:
        votes = article.get('votes', {})
        positive = votes.get('positive', 0) + votes.get('important', 0) * 1.5
        negative = votes.get('negative', 0) + votes.get('toxic', 0) * 1.5
        liked = votes.get('liked', 0)
        disliked = votes.get('disliked', 0)
        total_votes = positive + negative + liked + disliked
        if total_votes < MIN_RELIABLE_VOTES:
            article_id = article.get('id')
            title = article.get('title', '').strip()
            if article_id and title:
                low_vote_articles.append({'id': article_id, 'title': title})

    if not low_vote_articles:
        print("No low-vote articles to analyze with LLM")
        return {}

    print(f"Analyzing {len(low_vote_articles)} low-vote articles with Bedrock LLM")

    try:
        # タイトルリストを構築（最大50件に制限）
        titles_for_llm = low_vote_articles[:50]
        titles_text = '\n'.join(
            f'{i+1}. {a["title"]}' for i, a in enumerate(titles_for_llm)
        )

        prompt = f"""Analyze the sentiment of these crypto news article titles.
For each title, provide a sentiment score from 0.0 (very bearish) to 1.0 (very bullish), with 0.5 being neutral.

Consider:
- Regulatory actions, bans, lawsuits → bearish
- ETF approvals, institutional adoption, partnerships → bullish
- Hacks, exploits, fraud → bearish
- Price milestones, ATH, breakouts → bullish
- "Buy the dip", whale accumulation → bullish despite negative words
- Market uncertainty, FUD → mildly bearish
- Neutral news (updates, releases without clear impact) → 0.5

Titles:
{titles_text}

Respond with ONLY a JSON array of numbers in the same order. Example: [0.72, 0.35, 0.50]"""

        # Converse API（モデル非依存の統一API）
        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {"role": "user", "content": [{"text": prompt}]}
            ],
            inferenceConfig={
                "maxTokens": 256,
                "temperature": 0.0,
            }
        )

        content = response['output']['message']['content'][0]['text'].strip()

        # JSON配列をパース
        # LLMが余分なテキストを返す場合に備えて、最初の [ ... ] を抽出
        start = content.find('[')
        end = content.rfind(']') + 1
        if start >= 0 and end > start:
            scores_list = json.loads(content[start:end])
        else:
            print(f"LLM response not valid JSON array: {content[:200]}")
            return {}

        # スコアを記事IDにマッピング
        llm_scores = {}
        for i, article in enumerate(titles_for_llm):
            if i < len(scores_list):
                score = float(scores_list[i])
                # 0.0-1.0 にクランプ
                score = max(0.0, min(1.0, score))
                llm_scores[article['id']] = score

        input_tokens = response.get('usage', {}).get('inputTokens', 0)
        output_tokens = response.get('usage', {}).get('outputTokens', 0)
        print(f"LLM sentiment analysis complete: {len(llm_scores)} scores "
              f"(tokens: in={input_tokens}, out={output_tokens})")

        return llm_scores

    except Exception as e:
        print(f"Bedrock LLM sentiment analysis failed, falling back to rule-based: {e}")
        traceback.print_exc()
        return {}


def analyze_sentiment_weighted(news: list, llm_scores: dict = None) -> tuple:
    """時間加重センチメント分析（投票信頼性考慮 + LLMフォールバック）"""
    if not news:
        return 0.5, 0, {}

    if llm_scores is None:
        llm_scores = {}

    current_time = time.time()
    total_weighted_score = 0
    total_weight = 0
    fresh_count = 0

    vote_reliable_count = 0
    vote_unreliable_count = 0

    for article in news:
        try:
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
                # 投票データ不足時はLLMスコアを優先、フォールバックでルールベースNLP
                article_id = article.get('id')
                if article_id and article_id in llm_scores:
                    article_score = llm_scores[article_id]
                else:
                    article_score = estimate_sentiment_from_title(article.get('title', ''))

            # panic_score があれば補助的に使用
            # API v2: panic_score は 0〜100 の整数（市場重要度/インパクト）
            # ※ Growth Plan では panic_period パラメータ非対応のため通常 null
            panic_score = article.get('panic_score')
            if panic_score is not None and isinstance(panic_score, (int, float)):
                # 0-100スケール → 50を中立とし、±0.10 の微調整
                panic_adjustment = (panic_score - 50) / 500  # 0→-0.10, 50→0, 100→+0.10
                article_score = max(0.0, min(1.0, article_score + panic_adjustment))

            weight = time_weight * currency_weight
            total_weighted_score += article_score * weight
            total_weight += weight

        except Exception as e:
            print(f"Error analyzing article sentiment: {str(e)}")
            continue

    if total_weight == 0:
        return 0.5, fresh_count, {}

    final_score = total_weighted_score / total_weight

    stats = {
        'total_articles': len(news),
        'vote_reliable': vote_reliable_count,
        'vote_unreliable': vote_unreliable_count
    }

    return final_score, fresh_count, stats


def convert_to_jst(published_at: str) -> str:
    """ISO 8601 の published_at を JST (UTC+9) 文字列に変換"""
    if not published_at:
        return ''
    try:
        dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        dt_jst = dt.astimezone(JST)
        return dt_jst.strftime('%Y-%m-%d %H:%M:%S JST')
    except Exception as e:
        print(f"Error converting to JST '{published_at}': {str(e)}")
        return ''


def get_article_age_hours(published_at: str) -> float:
    """記事の経過時間（時間単位）"""
    if not published_at:
        return 24

    try:
        published = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        now = datetime.now(published.tzinfo)
        age_seconds = (now - published).total_seconds()
        return max(0, age_seconds / 3600)
    except Exception as e:
        print(f"Error parsing published_at '{published_at}': {str(e)}")
        return 24


def estimate_sentiment_from_title(title: str) -> float:
    """
    タイトルから高度なルールベース NLP でセンチメントを推定
    
    改善点 (Phase 2 #17):
    - 3段階の強度 (strong/moderate/mild) で重み分け
    - 否定語の検出 (not, no, fails, without → 極性反転)
    - バイグラム/フレーズマッチング
    - 暗号通貨ドメイン特化の語彙
    - 複数キーワードの相乗効果
    """
    if not title:
        return 0.5

    try:
        title_lower = title.lower()
        words = title_lower.split()

        # ===== フレーズマッチング (バイグラム/トリグラム優先) =====
        bullish_phrases = {
            # strong (+0.25)
            'all-time high': 0.25, 'all time high': 0.25, 'new ath': 0.25,
            'etf approved': 0.25, 'etf approval': 0.25, 'mass adoption': 0.25,
            'short squeeze': 0.25, 'whale accumulation': 0.20,
            'institutional buying': 0.20, 'record inflow': 0.20,
            # moderate (+0.15)
            'golden cross': 0.15, 'breaks out': 0.15, 'breaks above': 0.15,
            'price target': 0.15, 'buy signal': 0.15, 'strong support': 0.15,
            'higher high': 0.15, 'bullish divergence': 0.15,
            'network upgrade': 0.12, 'strategic reserve': 0.12,
            # contextual bullish (bearish word in bullish context)
            'buy the dip': 0.15, 'buying the dip': 0.15, 'bought the dip': 0.15,
            'buys the dip': 0.15, 'accumulate on dip': 0.12,
            'whales buy': 0.12, 'whales buying': 0.12, 'whale buying': 0.12,
            'bottom is in': 0.15, 'found support': 0.12, 'holds support': 0.12,
            'signs of recovery': 0.15, 'showing strength': 0.12,
        }
        bearish_phrases = {
            # strong (-0.25)
            'death cross': 0.25, 'bank run': 0.25, 'rug pull': 0.25,
            'ponzi scheme': 0.25, 'sec lawsuit': 0.25, 'exchange hack': 0.25,
            'mass liquidation': 0.25, 'flash crash': 0.25,
            # moderate (-0.15)
            'breaks below': 0.15, 'sell signal': 0.15, 'lower low': 0.15,
            'bearish divergence': 0.15, 'key support': 0.15, 'lost support': 0.15,
            'whale dump': 0.15, 'record outflow': 0.15, 'under investigation': 0.15,
            'class action': 0.15, 'security breach': 0.15,
        }

        phrase_score = 0.0
        matched_phrase = False
        for phrase, weight in bullish_phrases.items():
            if phrase in title_lower:
                phrase_score += weight
                matched_phrase = True
        for phrase, weight in bearish_phrases.items():
            if phrase in title_lower:
                phrase_score -= weight
                matched_phrase = True

        # ===== 単語レベル (強度別) =====
        strong_bullish = [
            'surge', 'soar', 'skyrocket', 'explode', 'moon', 'parabolic',
        ]
        moderate_bullish = [
            'rally', 'breakout', 'bullish', 'pump', 'gain', 'jump', 'boost',
            'adoption', 'partnership', 'upgrade', 'approval', 'inflow',
            'accumulate', 'outperform', 'momentum', 'recovery', 'rebound',
            'reclaim', 'optimistic', 'milestone', 'halving',
        ]
        mild_bullish = [
            'rise', 'climb', 'advance', 'positive', 'support', 'buying',
            'uptrend', 'upside', 'opportunity', 'growth', 'strengthen',
        ]

        strong_bearish = [
            'crash', 'plunge', 'collapse', 'tank', 'devastate', 'implode',
        ]
        moderate_bearish = [
            'dump', 'bearish', 'selloff', 'sell-off', 'decline', 'drop',
            'slump', 'hack', 'exploit', 'vulnerability', 'fraud', 'scam',
            'ban', 'crackdown', 'lawsuit', 'outflow', 'liquidat',
            'panic', 'warning', 'bubble', 'overvalued', 'correction',
            'bankrupt', 'insolvent', 'delisted',
        ]
        mild_bearish = [
            'fall', 'dip', 'slide', 'weak', 'fear', 'risk', 'concern',
            'uncertain', 'volatile', 'downtrend', 'resistance', 'struggle',
            'caution', 'fud', 'restriction',
        ]

        # 否定語リスト
        negation_words = {'not', 'no', 'never', "n't", 'without', 'fails',
                          'failed', 'unlikely', 'hardly', 'barely', 'neither'}

        def is_negated(word_idx: int) -> bool:
            """指定位置の単語が直前3語以内の否定語に修飾されているか"""
            for j in range(max(0, word_idx - 3), word_idx):
                w = words[j].rstrip('.,!?:;')
                if w in negation_words or w.endswith("n't"):
                    return True
            return False

        word_score = 0.0
        weights = {
            'strong_bullish': 0.20, 'moderate_bullish': 0.12, 'mild_bullish': 0.06,
            'strong_bearish': 0.20, 'moderate_bearish': 0.12, 'mild_bearish': 0.06,
        }

        for i, word in enumerate(words):
            w = word.rstrip('.,!?:;')
            negated = is_negated(i)

            if w in strong_bullish:
                delta = weights['strong_bullish'] * (-1 if negated else 1)
                word_score += delta
            elif w in moderate_bullish:
                delta = weights['moderate_bullish'] * (-1 if negated else 1)
                word_score += delta
            elif w in mild_bullish:
                delta = weights['mild_bullish'] * (-1 if negated else 1)
                word_score += delta
            elif w in strong_bearish:
                delta = weights['strong_bearish'] * (-1 if negated else 1)
                word_score -= delta
            elif w in moderate_bearish:
                delta = weights['moderate_bearish'] * (-1 if negated else 1)
                word_score -= delta
            elif w in mild_bearish:
                delta = weights['mild_bearish'] * (-1 if negated else 1)
                word_score -= delta

        # ===== 最終スコア: フレーズ + 単語、上限 ±0.4 =====
        net_score = phrase_score + word_score
        net_score = max(-0.4, min(0.4, net_score))
        return 0.5 + net_score

    except Exception as e:
        print(f"Error estimating sentiment from title '{title}': {str(e)}")
        return 0.5


def extract_top_headlines(articles: list, llm_scores: dict, top_n: int = 3, overall_score: float = 0.5) -> list:
    """センチメント方向を説明するニュースタイトル上位N件を抽出

    overall_score の方向に沿ったヘッドラインを優先表示:
    - bearish (< 0.45): スコアが低い順（なぜ弱気かを説明）
    - bullish (> 0.55): スコアが高い順（なぜ強気かを説明）
    - neutral: 影響度（中立からの乖離）順
    """
    scored_articles = []
    for article in articles:
        title = article.get('title', '').strip()
        if not title:
            continue

        votes = article.get('votes', {})
        positive = votes.get('positive', 0) + votes.get('important', 0) * 1.5
        negative = votes.get('negative', 0) + votes.get('toxic', 0) * 1.5
        liked = votes.get('liked', 0)
        disliked = votes.get('disliked', 0)
        total_votes = positive + negative + liked + disliked

        if total_votes >= MIN_RELIABLE_VOTES:
            article_score = (positive + liked) / total_votes
        else:
            article_id = article.get('id')
            if article_id and article_id in llm_scores:
                article_score = llm_scores[article_id]
            else:
                article_score = estimate_sentiment_from_title(title)

        scored_articles.append({
            'title': title[:120],
            'score': round(article_score, 2),
            'source': article.get('_source_currency', ''),
            'published_at_jst': convert_to_jst(article.get('published_at', '')),
        })

    # overall_score の方向に沿った記事を優先
    if overall_score < 0.45:
        # 弱気: スコアが低い記事を優先表示（なぜ弱気かを説明）
        scored_articles.sort(key=lambda x: x['score'])
    elif overall_score > 0.55:
        # 強気: スコアが高い記事を優先表示（なぜ強気かを説明）
        scored_articles.sort(key=lambda x: x['score'], reverse=True)
    else:
        # 中立: 影響度（中立からの乖離）順
        scored_articles.sort(key=lambda x: abs(x['score'] - 0.5), reverse=True)
    return scored_articles[:top_n]


def save_sentiment(pair: str, timestamp: int, score: float, news_count: int, fresh_count: int, top_headlines: list = None):
    """センチメント保存"""
    try:
        table = dynamodb.Table(SENTIMENT_TABLE)
        item = {
            'pair': pair,
            'timestamp': timestamp,
            'score': Decimal(str(round(score, 4))),
            'news_count': news_count,
            'fresh_news_count': fresh_count,
            'source': 'cryptopanic',
            'ttl': timestamp + 1209600  # 14日後に削除
        }
        if top_headlines:
            item['top_headlines'] = [
                {
                    'title': h['title'],
                    'score': Decimal(str(h['score'])),
                    'source': h.get('source', ''),
                    'published_at_jst': h.get('published_at_jst', ''),
                }
                for h in top_headlines
            ]
        table.put_item(Item=item)
    except Exception as e:
        print(f"Error saving sentiment for {pair}: {str(e)}")
        raise
