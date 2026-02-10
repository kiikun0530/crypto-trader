"""
マーケットコンテキスト収集 Lambda
30分間隔で市場全体のマクロ指標を収集・保存

データソース (全て無料API):
1. Fear & Greed Index (Alternative.me) — 市場の感情指標 0-100
2. ファンディングレート (Binance) — レバレッジポジション偏り
3. BTC Dominance (CoinGecko) — 資金フロー方向

出力: -1.0 ~ +1.0 のスコア (正=買い有利、負=売り有利)
"""
import json
import os
import time
import urllib.request
import boto3
from decimal import Decimal
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
MARKET_CONTEXT_TABLE = os.environ.get('MARKET_CONTEXT_TABLE', 'eth-trading-market-context')

# 6通貨のBinance先物シンボル
FUNDING_SYMBOLS = ['ETHUSDT', 'BTCUSDT', 'XRPUSDT', 'SOLUSDT', 'DOGEUSDT', 'AVAXUSDT']


def handler(event, context):
    """マーケットコンテキスト収集メイン"""
    timestamp = int(time.time())
    print(f"Starting market context collection at timestamp: {timestamp}")

    try:
        # 1. Fear & Greed Index取得
        print("Fetching Fear & Greed Index...")
        fng_data = fetch_fear_greed()
        print(f"Fear & Greed: value={fng_data['value']}, classification={fng_data['classification']}")

        # 2. ファンディングレート取得 (6通貨)
        print("Fetching funding rates...")
        funding_data = fetch_funding_rates()
        print(f"Funding rates: {json.dumps(funding_data['rates'])}")

        # 3. BTC Dominance取得
        print("Fetching BTC dominance...")
        dominance_data = fetch_btc_dominance()
        print(f"BTC Dominance: {dominance_data['btc_dominance']:.2f}%")

        # 4. 統合スコア計算
        print("Calculating market score...")
        market_score, components = calculate_market_score(fng_data, funding_data, dominance_data)
        print(f"Market Context Score: {market_score:+.4f}")

        # 5. DynamoDBに保存
        print("Saving to DynamoDB...")
        save_market_context(timestamp, market_score, components, fng_data, funding_data, dominance_data)
        print("Successfully saved market context data")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'market_score': round(market_score, 4),
                'components': components,
                'fear_greed': fng_data,
                'funding': funding_data,
                'btc_dominance': dominance_data,
                'timestamp': timestamp
            })
        }

    except Exception as e:
        print(f"Error in handler: {str(e)}")
        import traceback
        traceback.print_exc()
        # エラー時は中立スコアを保存
        try:
            save_market_context(timestamp, 0.0, {
                'fear_greed_score': 0.0,
                'funding_score': 0.0,
                'dominance_score': 0.0
            }, {}, {}, {})
            print("Saved neutral fallback data")
        except Exception as save_error:
            print(f"Failed to save fallback data: {str(save_error)}")
        
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e), 'market_score': 0.0})
        }


def fetch_fear_greed() -> dict:
    """
    Fear & Greed Index取得 (Alternative.me)
    0 = Extreme Fear, 100 = Extreme Greed
    無料・認証不要
    """
    try:
        url = 'https://api.alternative.me/fng/?limit=1'
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')
        req.add_header('Accept', 'application/json')

        print(f"Requesting Fear & Greed from: {url}")
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            fng = data.get('data', [{}])[0]
            result = {
                'value': int(fng.get('value', 50)),
                'classification': fng.get('value_classification', 'Neutral'),
                'timestamp': int(fng.get('timestamp', 0))
            }
            print(f"Successfully fetched Fear & Greed data: {result}")
            return result
    except Exception as e:
        print(f"Fear & Greed API error: {e}")
        fallback = {'value': 50, 'classification': 'Neutral', 'timestamp': 0}
        print(f"Using fallback Fear & Greed data: {fallback}")
        return fallback


def fetch_funding_rates() -> dict:
    """
    ファンディングレート取得 (Binance Futures)
    正 = ロングがショートに支払い (ロング過多 = 過熱)
    負 = ショートがロングに支払い (ショート過多 = 売られすぎ)
    無料・認証不要 (公開マーケットデータ)
    """
    rates = {}
    avg_rate = 0.0
    successful_fetches = 0

    for symbol in FUNDING_SYMBOLS:
        try:
            url = f'https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1'
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')
            req.add_header('Accept', 'application/json')

            print(f"Requesting funding rate for {symbol}...")
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                if data and len(data) > 0:
                    rate = float(data[0].get('fundingRate', 0))
                    rates[symbol] = rate
                    successful_fetches += 1
                    print(f"Successfully fetched {symbol}: {rate}")
                else:
                    print(f"Empty response for {symbol}")
                    rates[symbol] = 0.0
        except Exception as e:
            print(f"Funding rate error ({symbol}): {e}")
            rates[symbol] = 0.0

    if rates and successful_fetches > 0:
        avg_rate = sum(rates.values()) / len(rates)
        print(f"Calculated average funding rate: {avg_rate} (from {successful_fetches}/{len(FUNDING_SYMBOLS)} symbols)")
    else:
        print("Failed to fetch any funding rates, using 0.0")

    return {
        'rates': rates,
        'avg_rate': avg_rate
    }


def fetch_btc_dominance() -> dict:
    """
    BTC Dominance取得 (CoinGecko)
    Dominance上昇 = リスクオフ (アルトに不利)
    Dominance低下 = リスクオン (アルトに有利)
    無料Demo: 30回/分, 10K/月
    """
    try:
        url = 'https://api.coingecko.com/api/v3/global'
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'CryptoTrader-Bot/1.0')
        req.add_header('Accept', 'application/json')

        print(f"Requesting BTC dominance from: {url}")
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            market_data = data.get('data', {}).get('market_cap_percentage', {})
            btc_dom = market_data.get('btc', 50.0)
            eth_dom = market_data.get('eth', 10.0)
            total_cap = data.get('data', {}).get('total_market_cap', {}).get('usd', 0)
            
            result = {
                'btc_dominance': btc_dom,
                'eth_dominance': eth_dom,
                'total_market_cap': total_cap
            }
            print(f"Successfully fetched dominance data: BTC={btc_dom:.2f}%, ETH={eth_dom:.2f}%")
            return result
    except Exception as e:
        print(f"CoinGecko API error: {e}")
        fallback = {'btc_dominance': 50.0, 'eth_dominance': 10.0, 'total_market_cap': 0}
        print(f"Using fallback dominance data: {fallback}")
        return fallback


def calculate_market_score(fng_data: dict, funding_data: dict, dominance_data: dict) -> tuple:
    """
    マーケットコンテキストの統合スコアを計算

    返り値: (score: float, components: dict)
    score: -1.0 ~ +1.0
      正 = 市場環境がBUYに有利
      負 = 市場環境がBUYに不利

    コンポーネント重み:
    - Fear & Greed: 50% (最も強い市場シグナル)
    - ファンディングレート: 30% (レバレッジの過熱度)
    - BTC Dominance変化: 20% (資金フロー方向)
    """

    # === 1. Fear & Greed → スコア変換 ===
    # 逆張り: 極端な恐怖は買い機会、極端な貪欲は危険
    # ただし、中間域(30-70)では方向性を加味
    fng_value = fng_data.get('value', 50)
    print(f"Processing FNG value: {fng_value}")

    if fng_value <= 10:
        # Extreme Fear (0-10): 強い逆張り買いシグナル
        fng_score = 0.5 + (10 - fng_value) * 0.05  # +0.50 ~ +1.00
    elif fng_value <= 25:
        # Fear (11-25): 買い目
        fng_score = 0.1 + (25 - fng_value) * 0.027  # +0.10 ~ +0.50
    elif fng_value <= 45:
        # Mild Fear (26-45): やや買い
        fng_score = (45 - fng_value) * 0.005  # 0.0 ~ +0.10
    elif fng_value <= 55:
        # Neutral (46-55): 中立
        fng_score = 0.0
    elif fng_value <= 75:
        # Greed (56-75): やや売り
        fng_score = -(fng_value - 55) * 0.005  # 0.0 ~ -0.10
    elif fng_value <= 90:
        # High Greed (76-90): 売り目
        fng_score = -0.1 - (fng_value - 75) * 0.027  # -0.10 ~ -0.50
    else:
        # Extreme Greed (91-100): 強い逆張り売りシグナル
        fng_score = -0.5 - (fng_value - 90) * 0.05  # -0.50 ~ -1.00

    print(f"FNG score: {fng_score}")

    # === 2. ファンディングレート → スコア変換 ===
    # 正のファンディング = ロング過多 → BUYに不利 (過熱)
    # 負のファンディング = ショート過多 → BUY有利 (売られすぎ)
    # 通常レンジ: -0.01% ~ +0.01% (8h)
    avg_funding = funding_data.get('avg_rate', 0.0)
    print(f"Processing funding rate: {avg_funding}")

    # ファンディングレートを -1 ~ +1 にスケーリング
    # ±0.05% (= 0.0005) で ±1.0 にクリップ
    # 逆符号: 正のファンディング → 負のスコア (ロング過多は危険)
    funding_score = -avg_funding / 0.0005
    funding_score = max(-1.0, min(1.0, funding_score))
    print(f"Funding score: {funding_score}")

    # === 3. BTC Dominance → スコア変換 ===
    # BTC Dominance自体は方向性の指標
    # 高い(>60%): リスクオフ、アルトに不利
    # 低い(<40%): リスクオン、アルトに有利
    # 50%付近: 中立
    btc_dom = dominance_data.get('btc_dominance', 50.0)
    print(f"Processing BTC dominance: {btc_dom}%")

    # 50%を中立として、±15%で±1.0
    # < 50% = アルトに有利 = 正のスコア
    # > 50% = アルトに不利 = 負のスコア (BTCは別の判定)
    dominance_score = -(btc_dom - 50.0) / 15.0
    dominance_score = max(-1.0, min(1.0, dominance_score))
    print(f"Dominance score: {dominance_score}")

    # === 統合 ===
    # Fear & Greed: 50%, Funding: 30%, Dominance: 20%
    FNG_WEIGHT = 0.50
    FUNDING_WEIGHT = 0.30
    DOMINANCE_WEIGHT = 0.20

    market_score = (
        fng_score * FNG_WEIGHT +
        funding_score * FUNDING_WEIGHT +
        dominance_score * DOMINANCE_WEIGHT
    )

    components = {
        'fear_greed_score': round(fng_score, 4),
        'funding_score': round(funding_score, 4),
        'dominance_score': round(dominance_score, 4)
    }

    print(f"Final market score: {market_score} from components: {components}")
    return round(market_score, 4), components


def save_market_context(timestamp: int, score: float, components: dict,
                        fng_data: dict, funding_data: dict, dominance_data: dict):
    """マーケットコンテキストをDynamoDBに保存"""
    try:
        table = dynamodb.Table(MARKET_CONTEXT_TABLE)
        print(f"Preparing to save to table: {MARKET_CONTEXT_TABLE}")

        item = {
            'context_type': 'global',  # パーティションキー (市場全体なので固定)
            'timestamp': timestamp,
            'market_score': Decimal(str(round(score, 4))),
            'fng_score': Decimal(str(components.get('fear_greed_score', 0))),
            'funding_score': Decimal(str(components.get('funding_score', 0))),
            'dominance_score': Decimal(str(components.get('dominance_score', 0))),
            'fng_value': fng_data.get('value', 50) if fng_data else 50,
            'fng_classification': fng_data.get('classification', 'Neutral') if fng_data else 'Neutral',
            'avg_funding_rate': Decimal(str(round(funding_data.get('avg_rate', 0), 8))) if funding_data else Decimal('0'),
            'btc_dominance': Decimal(str(round(dominance_data.get('btc_dominance', 50), 2))) if dominance_data else Decimal('50'),
            'ttl': timestamp + 1209600  # 14日後に削除
        }

        # ファンディングレートの個別値
        if funding_data and funding_data.get('rates'):
            for symbol, rate in funding_data['rates'].items():
                item[f'funding_{symbol.lower().replace("usdt", "")}'] = Decimal(str(round(rate, 8)))

        print(f"Saving item with keys: {list(item.keys())}")
        table.put_item(Item=item)
        print(f"Successfully saved market context: score={score:+.4f}, fng={fng_data.get('value', '?')}, "
              f"funding={funding_data.get('avg_rate', 0):.6f}, btc_dom={dominance_data.get('btc_dominance', '?')}%")

    except ClientError as e:
        print(f"DynamoDB ClientError: {e.response['Error']['Code']} - {e.response['Error']['Message']}")
        raise
    except Exception as e:
        print(f"Error saving to DynamoDB: {str(e)}")
        raise
