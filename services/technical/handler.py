"""
テクニカル分析 Lambda
RSI, MACD, SMA20/200, ボリンジャーバンド, ADX, ATRを計算
5分足250本 = 約20時間分のデータを使用

レジーム検知:
- ADXでトレンド強度を判定
- トレンド相場: MACD/SMAのウェイト強化
- レンジ相場: RSI/BBのウェイト強化
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
        adx = calculate_adx(close_prices, 14)
        atr = calculate_atr(close_prices, 14)
        
        # レジーム検知: ADXでトレンド強度判定
        # ADX > 25: トレンド相場, ADX < 20: レンジ相場
        regime = 'trending' if adx > 25 else ('ranging' if adx < 20 else 'neutral')
        
        # スコア計算（-1 to 1）- レジーム適応型
        score = calculate_score(close_prices[-1], rsi, macd, signal, sma_20, sma_200, bb_upper, bb_lower, regime)
        
        indicators = {
            'rsi': round(rsi, 2),
            'macd': round(macd, 4),
            'macd_signal': round(signal, 4),
            'macd_histogram': round(histogram, 4),
            'sma_20': round(sma_20, 2),
            'bb_upper': round(bb_upper, 2),
            'bb_lower': round(bb_lower, 2),
            'adx': round(adx, 2),
            'atr': round(atr, 4),
            'atr_percent': round(atr / close_prices[-1] * 100, 3) if close_prices[-1] > 0 else 0,
            'regime': regime,
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
    """MACD計算（EMA(9)シグナルライン）"""
    if len(prices) < slow:
        return 0.0, 0.0, 0.0

    # 全期間のMACD系列を計算してEMA(9)シグナルラインを求める
    macd_series = calculate_macd_series(prices, fast, slow)
    macd_line = macd_series[-1]

    # シグナルライン = MACD系列のEMA(signal_period)
    if len(macd_series) >= signal_period:
        signal_line = calculate_ema(macd_series, signal_period)
    else:
        signal_line = sum(macd_series) / len(macd_series)

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_macd_series(prices: list, fast: int = 12, slow: int = 26) -> list:
    """全期間のMACD値リストを計算（シグナルラインEMA算出用）"""
    if len(prices) < slow:
        return [0.0]

    multiplier_fast = 2 / (fast + 1)
    multiplier_slow = 2 / (slow + 1)

    # EMA初期値（SMA）
    ema_fast = sum(prices[:fast]) / fast
    ema_slow = sum(prices[:slow]) / slow

    # slow期間のEMA_fastを先に進める
    for price in prices[fast:slow]:
        ema_fast = (price - ema_fast) * multiplier_fast + ema_fast

    macd_series = [ema_fast - ema_slow]

    # slow以降で両方のEMAを更新
    for price in prices[slow:]:
        ema_fast = (price - ema_fast) * multiplier_fast + ema_fast
        ema_slow = (price - ema_slow) * multiplier_slow + ema_slow
        macd_series.append(ema_fast - ema_slow)

    return macd_series

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
                   sma_20: float, sma_200: float, bb_upper: float, bb_lower: float,
                   regime: str = 'neutral') -> float:
    """
    総合スコア計算 (-1 to 1)
    
    レジーム適応型ウェイト:
    - トレンド相場 (ADX>25): MACD/SMA重視 (トレンドフォロー)
    - レンジ相場 (ADX<20): RSI/BB重視 (逆張り)
    - 中立: 均等ウェイト
    """
    # レジーム別ウェイト
    if regime == 'trending':
        # トレンド相場: MACD/SMA重視、RSI/BB軽視
        w_rsi, w_macd, w_sma, w_bb = 0.15, 0.35, 0.35, 0.15
    elif regime == 'ranging':
        # レンジ相場: RSI/BB重視（逆張り）、MACD/SMA軽視
        w_rsi, w_macd, w_sma, w_bb = 0.35, 0.15, 0.15, 0.35
    else:
        # 中立: 均等
        w_rsi, w_macd, w_sma, w_bb = 0.25, 0.25, 0.25, 0.25
    
    score = 0.0
    
    # RSI (30以下: 買い、70以上: 売り)
    if rsi < 30:
        score += w_rsi
    elif rsi > 70:
        score -= w_rsi
    else:
        score += (50 - rsi) / 200 * (w_rsi / 0.25)  # ウェイトに応じてスケール
    
    # MACD (シグナル上抜け: 買い)
    if macd > signal:
        score += w_macd
    else:
        score -= w_macd
    
    # SMA20/200 ゴールデン/デッドクロス
    if sma_200:
        if sma_20 > sma_200:
            score += w_sma  # ゴールデンクロス（上昇トレンド）
        else:
            score -= w_sma  # デッドクロス（下降トレンド）
    else:
        # SMA200がない場合はSMA20との位置関係で判断
        if current_price > sma_20:
            score += w_sma * 0.6
        else:
            score -= w_sma * 0.6
    
    # ボリンジャーバンド (線形グラデーション - デッドゾーン解消)
    # 旧: 0.2-0.8はスコア0 → 新: 全範囲で線形スコア
    if bb_upper != bb_lower:
        bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
        # 0.0=下限 → +w_bb, 0.5=中央 → 0, 1.0=上限 → -w_bb
        bb_score = (0.5 - bb_position) * 2 * w_bb
        # 極端な位置でボーナス（バンド外）
        if bb_position < 0.0 or bb_position > 1.0:
            bb_score *= 1.2
        score += max(-w_bb * 1.2, min(w_bb * 1.2, bb_score))
    
    return max(-1, min(1, score))


def calculate_atr(prices: list, period: int = 14) -> float:
    """
    ATR (Average True Range) 計算
    
    単一価格系列（close）から近似計算。
    本来はHigh/Low/Closeが必要だが、5分足closeのみの環境では
    連続する2本のclose差の絶対値の移動平均で代用。
    """
    if len(prices) < period + 1:
        return 0.0
    
    true_ranges = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    
    # Wilder's smoothed average (exponential)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    
    return atr


def calculate_adx(prices: list, period: int = 14) -> float:
    """
    ADX (Average Directional Index) 計算
    
    トレンド強度の指標 (0-100):
    - ADX > 25: 強いトレンド相場
    - ADX < 20: レンジ相場
    - 20-25: 転換期
    
    単一価格系列から近似:
    - +DM: 上昇幅 (price[i] - price[i-1] > 0)
    - -DM: 下降幅 (price[i-1] - price[i] > 0)
    """
    if len(prices) < period * 2 + 1:
        return 20.0  # データ不足時はneutral
    
    # +DM, -DM 計算
    plus_dm = []
    minus_dm = []
    tr_list = []
    
    for i in range(1, len(prices)):
        up_move = prices[i] - prices[i-1]
        down_move = prices[i-1] - prices[i]
        
        # True Range (close-onlyの近似)
        tr = abs(prices[i] - prices[i-1])
        tr_list.append(tr)
        
        if up_move > 0 and up_move > down_move:
            plus_dm.append(up_move)
            minus_dm.append(0)
        elif down_move > 0 and down_move > up_move:
            plus_dm.append(0)
            minus_dm.append(down_move)
        else:
            plus_dm.append(0)
            minus_dm.append(0)
    
    if len(tr_list) < period:
        return 20.0
    
    # Wilder's smoothing
    smoothed_plus_dm = sum(plus_dm[:period])
    smoothed_minus_dm = sum(minus_dm[:period])
    smoothed_tr = sum(tr_list[:period])
    
    dx_list = []
    
    for i in range(period, len(plus_dm)):
        smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / period + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / period + minus_dm[i]
        smoothed_tr = smoothed_tr - smoothed_tr / period + tr_list[i]
        
        if smoothed_tr > 0:
            plus_di = 100 * smoothed_plus_dm / smoothed_tr
            minus_di = 100 * smoothed_minus_dm / smoothed_tr
        else:
            plus_di = 0
            minus_di = 0
        
        di_sum = plus_di + minus_di
        if di_sum > 0:
            dx = 100 * abs(plus_di - minus_di) / di_sum
        else:
            dx = 0
        dx_list.append(dx)
    
    if not dx_list:
        return 20.0
    
    # ADX = smoothed average of DX
    if len(dx_list) >= period:
        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = (adx * (period - 1) + dx) / period
        return adx
    else:
        return sum(dx_list) / len(dx_list)
