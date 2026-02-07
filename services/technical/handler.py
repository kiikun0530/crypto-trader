"""
テクニカル分析 Lambda
RSI, MACD, SMA20/200, ボリンジャーバンドを計算
5分足200本 = 約16時間分のデータを使用
"""
import json
import os
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')

def handler(event, context):
    """テクニカル分析実行"""
    pair = event.get('pair', 'eth_usdt')
    
    try:
        # 価格履歴取得（直近250件 - 余裕を持って）
        prices = get_price_history(pair, limit=250)
        
        if len(prices) < 50:
            return {
                'pair': pair,
                'technical_score': 0.5,
                'indicators': {},
                'reason': 'insufficient_data',
                'data_count': len(prices),
                'current_price': 0
            }
        
        # 各指標計算
        close_prices = [p['price'] for p in prices]
        
        rsi = calculate_rsi(close_prices, 14)
        macd, signal, histogram = calculate_macd(close_prices)
        sma_20 = calculate_sma(close_prices, 20)
        sma_200 = calculate_sma(close_prices, 200) if len(close_prices) >= 200 else None
        bb_upper, bb_lower = calculate_bollinger_bands(close_prices, 20, 2)
        
        # スコア計算（-1 to 1）
        score = calculate_score(close_prices[-1], rsi, macd, signal, sma_20, sma_200, bb_upper, bb_lower)
        
        indicators = {
            'rsi': round(rsi, 2),
            'macd': round(macd, 4),
            'macd_signal': round(signal, 4),
            'macd_histogram': round(histogram, 4),
            'sma_20': round(sma_20, 2),
            'bb_upper': round(bb_upper, 2),
            'bb_lower': round(bb_lower, 2),
            'current_price': close_prices[-1],
            'data_count': len(prices)
        }
        
        if sma_200:
            indicators['sma_200'] = round(sma_200, 2)
            indicators['golden_cross'] = sma_20 > sma_200  # ゴールデンクロス状態
        
        return {
            'pair': pair,
            'technical_score': round(score, 3),
            'indicators': indicators,
            'current_price': close_prices[-1]
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'pair': pair,
            'technical_score': 0.5,
            'error': str(e),
            'current_price': 0
        }

def get_price_history(pair: str, limit: int = 100) -> list:
    """価格履歴取得"""
    table = dynamodb.Table(PRICES_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=limit
    )
    items = response.get('Items', [])
    return [{'timestamp': int(i['timestamp']), 'price': float(i['price'])} for i in reversed(items)]

def calculate_rsi(prices: list, period: int = 14) -> float:
    """RSI計算"""
    if len(prices) < period + 1:
        return 50.0
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(prices: list, fast: int = 12, slow: int = 26, signal_period: int = 9) -> tuple:
    """MACD計算"""
    if len(prices) < slow:
        return 0.0, 0.0, 0.0
    
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    macd_line = ema_fast - ema_slow
    
    # シグナルライン（簡易版：直近9日の平均）
    signal_line = macd_line * 0.9  # 簡易計算
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram

def calculate_ema(prices: list, period: int) -> float:
    """EMA計算"""
    if len(prices) < period:
        return prices[-1]
    
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema

def calculate_sma(prices: list, period: int) -> float:
    """SMA計算"""
    if len(prices) < period:
        return prices[-1]
    return sum(prices[-period:]) / period

def calculate_bollinger_bands(prices: list, period: int = 20, std_dev: int = 2) -> tuple:
    """ボリンジャーバンド計算"""
    if len(prices) < period:
        return prices[-1] * 1.02, prices[-1] * 0.98
    
    sma = calculate_sma(prices, period)
    variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
    std = variance ** 0.5
    
    return sma + std_dev * std, sma - std_dev * std

def calculate_score(current_price: float, rsi: float, macd: float, signal: float,
                   sma_20: float, sma_200: float, bb_upper: float, bb_lower: float) -> float:
    """総合スコア計算 (-1 to 1)"""
    score = 0.0
    
    # RSI (30以下: 買い、70以上: 売り) - 重み 25%
    if rsi < 30:
        score += 0.25
    elif rsi > 70:
        score -= 0.25
    else:
        score += (50 - rsi) / 200  # 中立付近は弱いシグナル
    
    # MACD (シグナル上抜け: 買い) - 重み 25%
    if macd > signal:
        score += 0.25
    else:
        score -= 0.25
    
    # SMA20/200 ゴールデン/デッドクロス - 重み 25%
    if sma_200:
        if sma_20 > sma_200:
            score += 0.25  # ゴールデンクロス（上昇トレンド）
        else:
            score -= 0.25  # デッドクロス（下降トレンド）
    else:
        # SMA200がない場合はSMA20との位置関係で判断
        if current_price > sma_20:
            score += 0.15
        else:
            score -= 0.15
    
    # ボリンジャーバンド (下限付近: 買い、上限付近: 売り) - 重み 25%
    if bb_upper != bb_lower:
        bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
        if bb_position < 0.2:
            score += 0.25  # 下限付近 = 売られすぎ
        elif bb_position > 0.8:
            score -= 0.25  # 上限付近 = 買われすぎ
    
    return max(-1, min(1, score))
