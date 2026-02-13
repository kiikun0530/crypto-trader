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
# 0.5→0.35: 間接ニュースが直接ニュースを支配しないよう緩和
BTC_CORRELATION_WEIGHT = 0.35

# 全体市場ニュースの重み
# 0.3→0.20: 汎用ニュースの影響を低減
MARKET_NEWS_WEIGHT = 0.20

# 集約スコアの極端値ダンピング係数
# 0.5から離れるほどダンピングが効く（1.0=ダンピングなし）
SCORE_DAMPENING_FACTOR = 0.85


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

        prompt = f"""Analyze the sentiment of these crypto news article titles for their ACTUAL market impact.
For each title, provide a sentiment score from 0.0 (very bearish) to 1.0 (very bullish), with 0.5 being neutral.

IMPORTANT scoring guidelines:
- Use MODERATE scores (0.30-0.70) for most news. Reserve extreme scores for truly exceptional events.
- Confirmed events deserve stronger scores than rumors, predictions, or analyst opinions.
- Clickbait headlines ("could crash", "might explode", "warns of") → score closer to 0.50, do NOT overreact.
- Headlines with "?" are often speculative → lean toward 0.50.
- Minor price movements (1-5%) are normal volatility → score near 0.50.

Event calibration:
- Confirmed ETF approval, major hack, exchange collapse → 0.15 or 0.85 (rarely more extreme)
- Regulatory actions, partnerships, institutional adoption → 0.25-0.35 or 0.65-0.75
- Hacks, exploits, fraud → 0.20-0.30
- Price milestones, ATH → 0.65-0.75
- "Buy the dip", whale accumulation → 0.55-0.65 (mildly bullish)
- Market uncertainty, FUD → 0.40-0.45 (mildly bearish)
- Neutral news (updates, releases, routine) → 0.48-0.52
- Analyst predictions, rumors, speculation → 0.40-0.60 (near neutral)

Titles:
{titles_text}

Respond with ONLY a JSON array of numbers in the same order. Example: [0.58, 0.38, 0.50]"""

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
            # liked/disliked はエンゲージメント指標であり、センチメントとは限らないため
            # 重みを 0.3 に抑制（弱気ニュースにも「いいね」される場合がある）
            liked = votes.get('liked', 0) * 0.3
            disliked = votes.get('disliked', 0) * 0.3
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

    raw_score = total_weighted_score / total_weight

    # === 極端スコアのダンピング ===
    # 0.5から離れるほど抑制が効く。恐怖/貪欲期に全記事が同方向になる過剰反応を防止
    # 例: raw=0.2 → 0.5 + (0.2-0.5)*0.85 = 0.245 (やや中立寄りに)
    # 例: raw=0.8 → 0.5 + (0.8-0.5)*0.85 = 0.755
    final_score = 0.5 + (raw_score - 0.5) * SCORE_DAMPENING_FACTOR

    stats = {
        'total_articles': len(news),
        'vote_reliable': vote_reliable_count,
        'vote_unreliable': vote_unreliable_count,
        'raw_score': round(raw_score, 4),
        'dampened_score': round(final_score, 4),
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

    改善点 (Phase 3):
    - 3段階の強度 (strong/moderate/mild) で重み分け
    - 否定語の検出 (not, no, fails, without → 極性反転)
    - バイグラム/フレーズマッチング
    - クリックベイト/仮定表現の検出 → 重み減衰
    - pump and dump 等のコンテキスト判定
    - 接頭辞マッチング対応 (liquidat → liquidation, liquidated)
    - 1記事あたりのスコア上限を ±0.30 に制限（過剰反応防止）
    """
    if not title:
        return 0.5

    try:
        title_lower = title.lower()
        words = title_lower.split()

        # ===== クリックベイト/仮定/推測表現の検出 → 重み割引 =====
        speculative_phrases = [
            'could ', 'might ', 'may ', 'warns ', 'warns of', 'predicts ',
            'analyst says', 'analysts say', 'expert says', 'experts say',
            'according to', 'rumor', 'rumour', 'speculation', 'speculate',
            'if ', 'what if', 'will it', 'can it', 'should you',
            'opinion:', 'editorial:', '?',  # 疑問形の見出し
        ]
        is_speculative = any(sp in title_lower for sp in speculative_phrases)
        # 仮定表現なら重みを60%に減衰
        speculation_dampener = 0.6 if is_speculative else 1.0

        # ===== フレーズマッチング (バイグラム/トリグラム優先) =====
        bullish_phrases = {
            # strong (+0.20) — 確定的な強い事実
            'all-time high': 0.20, 'all time high': 0.20, 'new ath': 0.20,
            'etf approved': 0.20, 'etf approval': 0.20, 'mass adoption': 0.18,
            'short squeeze': 0.18, 'whale accumulation': 0.15,
            'institutional buying': 0.15, 'record inflow': 0.15,
            # moderate (+0.12)
            'golden cross': 0.12, 'breaks out': 0.12, 'breaks above': 0.12,
            'price target': 0.10, 'buy signal': 0.10, 'strong support': 0.10,
            'higher high': 0.10, 'bullish divergence': 0.12,
            'network upgrade': 0.08, 'strategic reserve': 0.10,
            # contextual bullish (bearish word in bullish context)
            'buy the dip': 0.10, 'buying the dip': 0.10, 'bought the dip': 0.10,
            'buys the dip': 0.10, 'accumulate on dip': 0.08,
            'whales buy': 0.08, 'whales buying': 0.08, 'whale buying': 0.08,
            'bottom is in': 0.10, 'found support': 0.08, 'holds support': 0.08,
            'signs of recovery': 0.10, 'showing strength': 0.08,
        }
        bearish_phrases = {
            # strong (-0.20) — 確定的な強い事実
            'death cross': 0.20, 'bank run': 0.20, 'rug pull': 0.20,
            'ponzi scheme': 0.20, 'sec lawsuit': 0.18, 'exchange hack': 0.20,
            'mass liquidation': 0.20, 'flash crash': 0.20,
            'pump and dump': 0.18, 'pump-and-dump': 0.18,
            # moderate (-0.12)
            'breaks below': 0.12, 'sell signal': 0.12, 'lower low': 0.12,
            'bearish divergence': 0.12, 'key support lost': 0.12, 'lost support': 0.12,
            'whale dump': 0.12, 'record outflow': 0.12, 'under investigation': 0.12,
            'class action': 0.12, 'security breach': 0.12,
        }

        phrase_score = 0.0
        matched_phrases = []
        for phrase, weight in bullish_phrases.items():
            if phrase in title_lower:
                phrase_score += weight * speculation_dampener
                matched_phrases.append(phrase)
        for phrase, weight in bearish_phrases.items():
            if phrase in title_lower:
                phrase_score -= weight * speculation_dampener
                matched_phrases.append(phrase)

        # ===== 単語レベル (強度別) =====
        # ※ strong のウェイトを緩和（クリックベイト耐性）
        strong_bullish = {
            'surge': 0.14, 'soar': 0.14, 'skyrocket': 0.14,
            'parabolic': 0.14,
        }
        moderate_bullish = {
            'rally': 0.09, 'breakout': 0.09, 'bullish': 0.09,
            'gain': 0.07, 'jump': 0.07, 'boost': 0.07,
            'adoption': 0.08, 'partnership': 0.06, 'upgrade': 0.05,
            'approval': 0.08, 'inflow': 0.07,
            'accumulate': 0.07, 'outperform': 0.07, 'momentum': 0.06,
            'recovery': 0.07, 'rebound': 0.07,
            'reclaim': 0.06, 'optimistic': 0.06, 'milestone': 0.04,
            'halving': 0.07,
        }
        mild_bullish = {
            'rise': 0.04, 'climb': 0.04, 'advance': 0.04,
            'positive': 0.04, 'support': 0.03, 'buying': 0.04,
            'uptrend': 0.04, 'upside': 0.04, 'opportunity': 0.03,
            'growth': 0.04, 'strengthen': 0.04,
        }

        strong_bearish = {
            'crash': 0.14, 'plunge': 0.14, 'collapse': 0.14,
            'tank': 0.14, 'devastate': 0.14, 'implode': 0.14,
        }
        moderate_bearish = {
            'dump': 0.09, 'bearish': 0.09, 'selloff': 0.09,
            'sell-off': 0.09, 'decline': 0.07, 'drop': 0.07,
            'slump': 0.08, 'hack': 0.09, 'exploit': 0.08,
            'vulnerability': 0.07, 'fraud': 0.09, 'scam': 0.09,
            'ban': 0.08, 'crackdown': 0.09, 'lawsuit': 0.08,
            'outflow': 0.07, 'panic': 0.07, 'warning': 0.05,
            'bubble': 0.07, 'overvalued': 0.07, 'correction': 0.05,
            'bankrupt': 0.09, 'insolvent': 0.09, 'delisted': 0.08,
        }
        mild_bearish = {
            'fall': 0.04, 'dip': 0.03, 'slide': 0.04,
            'weak': 0.04, 'fear': 0.04, 'risk': 0.03,
            'concern': 0.03, 'uncertain': 0.04, 'volatile': 0.03,
            'downtrend': 0.04, 'resistance': 0.03, 'struggle': 0.04,
            'caution': 0.03, 'fud': 0.04, 'restriction': 0.04,
        }

        # 接頭辞マッチング用リスト（活用形対応）
        bearish_prefixes = {
            'liquidat': 0.09,  # liquidation, liquidated, liquidating
            'investigat': 0.07,  # investigation, investigated
        }
        bullish_prefixes = {
            'accumulat': 0.07,  # accumulating, accumulated, accumulation
        }

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

        # コンテキスト判定: "pump and dump" はフレーズで処理済みなので単語 pump をスキップ
        pump_dump_context = 'pump and dump' in title_lower or 'pump-and-dump' in title_lower
        # "explode" / "moon" はクリックベイト率が高いので強度を下げて処理
        clickbait_words = {'explode': 0.06, 'moon': 0.04, 'moonshot': 0.04}

        word_score = 0.0

        for i, word in enumerate(words):
            w = word.rstrip('.,!?:;')
            negated = is_negated(i)

            # pump のコンテキスト判定
            if w == 'pump' and pump_dump_context:
                continue  # フレーズ側で処理済み

            # クリックベイト頻出語は専用の低い重みで処理
            if w in clickbait_words:
                delta = clickbait_words[w] * (-1 if negated else 1)
                word_score += delta * speculation_dampener
                continue

            # 通常の単語マッチ（辞書ベースで個別重み）
            matched = False
            if w in strong_bullish:
                delta = strong_bullish[w] * (-1 if negated else 1)
                word_score += delta * speculation_dampener
                matched = True
            elif w in moderate_bullish:
                delta = moderate_bullish[w] * (-1 if negated else 1)
                word_score += delta * speculation_dampener
                matched = True
            elif w in mild_bullish:
                delta = mild_bullish[w] * (-1 if negated else 1)
                word_score += delta * speculation_dampener
                matched = True
            elif w in strong_bearish:
                delta = strong_bearish[w] * (-1 if negated else 1)
                word_score -= delta * speculation_dampener
                matched = True
            elif w in moderate_bearish:
                delta = moderate_bearish[w] * (-1 if negated else 1)
                word_score -= delta * speculation_dampener
                matched = True
            elif w in mild_bearish:
                delta = mild_bearish[w] * (-1 if negated else 1)
                word_score -= delta * speculation_dampener
                matched = True

            # 接頭辞マッチング（活用形対応）
            if not matched:
                for prefix, weight in bearish_prefixes.items():
                    if w.startswith(prefix):
                        delta = weight * (-1 if negated else 1)
                        word_score -= delta * speculation_dampener
                        break
                for prefix, weight in bullish_prefixes.items():
                    if w.startswith(prefix):
                        delta = weight * (-1 if negated else 1)
                        word_score += delta * speculation_dampener
                        break

        # ===== 最終スコア: フレーズ + 単語、上限 ±0.30（過剰反応防止） =====
        net_score = phrase_score + word_score
        net_score = max(-0.30, min(0.30, net_score))
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
        liked = votes.get('liked', 0) * 0.3
        disliked = votes.get('disliked', 0) * 0.3
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
