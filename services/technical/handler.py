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
                'technical_score': 0.0,
                'indicators': {},
                'reason': 'insufficient_data',
                'data_count': len(prices),
                'current_price': 0
            }
        
        # 各指標計算
        close_prices = [p['price'] for p in prices]
        
        # OHLC データ抽出（存在する場合のみ — 古いレコードにはcloseのみ）
        has_ohlc = all('high' in p and 'low' in p for p in prices[-50:])
        if has_ohlc:
            highs = [p.get('high', p['price']) for p in prices]
            lows = [p.get('low', p['price']) for p in prices]
            opens = [p.get('open', p['price']) for p in prices]
        else:
            highs = None
            lows = None
            opens = None
        
        # Volume データ抽出（存在する場合のみ）
        volumes = [p.get('volume', 0) for p in prices]
        has_volume = any(v > 0 for v in volumes[-20:])
        
        rsi = calculate_rsi(close_prices, 14)
        macd, signal, histogram = calculate_macd(close_prices)
        macd_histogram_slope = calculate_macd_histogram_slope(close_prices)
        sma_20 = calculate_sma(close_prices, 20)
        sma_200 = calculate_sma(close_prices, 200) if len(close_prices) >= 200 else None
        bb_upper, bb_lower = calculate_bollinger_bands(close_prices, 20, 2)
        
        # ADX/ATR: OHLC があれば正確な計算、なければ close-only 近似
        adx = calculate_adx(close_prices, 14, highs=highs, lows=lows)
        atr = calculate_atr(close_prices, 14, highs=highs, lows=lows)
        
        # レジーム検知: ADXでトレンド強度判定
        # ADX > 25: トレンド相場, ADX < 20: レンジ相場
        regime = 'trending' if adx > 25 else ('ranging' if adx < 20 else 'neutral')
        
        # RSI売られすぎ/買われすぎ 継続期間を算出
        rsi_oversold_bars, rsi_overbought_bars = calculate_rsi_duration(close_prices, 14)
        
        # Volume 確認シグナル（出来高急増でスコア増幅）
        volume_multiplier = 1.0
        if has_volume and len(volumes) >= 20:
            volume_multiplier = calculate_volume_signal(volumes)
        
        # スコア計算（-1 to 1）- レジーム適応型
        score = calculate_score(close_prices[-1], rsi, macd, signal, sma_20, sma_200,
                                bb_upper, bb_lower, regime,
                                rsi_oversold_bars=rsi_oversold_bars,
                                rsi_overbought_bars=rsi_overbought_bars)
        
        # Volume による確認: 出来高増加時にスコアの方向性を強化
        if volume_multiplier > 1.0:
            score = max(-1, min(1, score * volume_multiplier))
        
        indicators = {
            'rsi': round(rsi, 2),
            'macd': round(macd, 4),
            'macd_signal': round(signal, 4),
            'macd_histogram': round(histogram, 4),
            'macd_histogram_slope': round(macd_histogram_slope, 4),
            'sma_20': round(sma_20, 2),
            'bb_upper': round(bb_upper, 2),
            'bb_lower': round(bb_lower, 2),
            'adx': round(adx, 2),
            'atr': round(atr, 4),
            'atr_percent': round(atr / close_prices[-1] * 100, 3) if close_prices[-1] > 0 else 0,
            'regime': regime,
            'rsi_oversold_bars': rsi_oversold_bars,
            'rsi_overbought_bars': rsi_overbought_bars,
            'current_price': close_prices[-1],
            'data_count': len(prices),
            'has_ohlc': has_ohlc,
            'volume_multiplier': round(volume_multiplier, 3)
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
            'technical_score': 0.0,
            'error': str(e),
            'current_price': 0
        }

def get_price_history(pair: str, limit: int = 100) -> list:
    """価格履歴取得（OHLCV対応）"""
    table = dynamodb.Table(PRICES_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=limit
    )
    items = response.get('Items', [])
    result = []
    for i in reversed(items):
        record = {
            'timestamp': int(i['timestamp']),
            'price': float(i['price'])
        }
        # OHLCV フィールドが存在する場合は追加
        if 'high' in i:
            record['high'] = float(i['high'])
        if 'low' in i:
            record['low'] = float(i['low'])
        if 'open' in i:
            record['open'] = float(i['open'])
        if 'volume' in i:
            record['volume'] = float(i['volume'])
        result.append(record)
    return result

def calculate_rsi(prices: list, period: int = 14) -> float:
    """
    RSI計算 (Wilder's Smoothed Moving Average)
    
    標準的なWilder方式:
    - 最初のperiod期間は単純平均で初期化
    - 以降は指数平滑化: avg = (prev_avg * (period-1) + current) / period
    - 単純平均より安定した値を返す
    """
    if len(prices) < period + 1:
        return 50.0
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    # Wilder's smoothed average: 最初のperiod期間はSMAで初期化
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    # 以降は指数平滑化
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
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

def calculate_rsi_duration(prices: list, period: int = 14) -> tuple:
    """
    RSI系列を計算し、直近の売られすぎ/買われすぎ継続バー数を返す
    
    Returns:
        (oversold_bars, overbought_bars):
        - oversold_bars: RSI < 30 が連続している5分足バー数 (0 = 現在は非売られすぎ)
        - overbought_bars: RSI > 70 が連続している5分足バー数
        5分足なので 12bars = 1時間、144bars = 12時間
    """
    if len(prices) < period + 2:
        return 0, 0
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    rsi_series = []
    # 最初のRSI
    if avg_loss == 0:
        rsi_series.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_series.append(100 - (100 / (1 + rs)))
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_series.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_series.append(100 - (100 / (1 + rs)))
    
    # 末尾から売られすぎ/買われすぎの連続数をカウント
    oversold_bars = 0
    for r in reversed(rsi_series):
        if r < 30:
            oversold_bars += 1
        else:
            break
    
    overbought_bars = 0
    for r in reversed(rsi_series):
        if r > 70:
            overbought_bars += 1
        else:
            break
    
    return oversold_bars, overbought_bars


def calculate_score(current_price: float, rsi: float, macd: float, signal: float,
                   sma_20: float, sma_200: float, bb_upper: float, bb_lower: float,
                   regime: str = 'neutral',
                   rsi_oversold_bars: int = 0, rsi_overbought_bars: int = 0) -> float:
    """
    総合スコア計算 (-1 to 1)
    
    レジーム適応型ウェイト:
    - トレンド相場 (ADX>25): MACD/SMA重視 (トレンドフォロー)
    - レンジ相場 (ADX<20): RSI/BB重視 (逆張り)
    - 中立: 均等ウェイト
    
    改善点:
    - RSI売られすぎ/買われすぎ継続時間による減衰
    - トレンド相場でのRSI逆張り抑制
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
    
    # --- RSI スコア計算（継続時間 + トレンド方向を考慮）---
    # 基本スコア
    if rsi < 30:
        rsi_raw = w_rsi  # 売られすぎ → 買いシグナル
    elif rsi > 70:
        rsi_raw = -w_rsi  # 買われすぎ → 売りシグナル
    else:
        rsi_raw = (50 - rsi) / 200 * (w_rsi / 0.25)
    
    # 改善1: 売られすぎ/買われすぎ継続時間による減衰
    # 12bars(1h)以上継続 → 50%に減衰、36bars(3h)以上 → 25%に減衰
    # 長期間の売られすぎは「落ちるナイフ」の可能性が高い
    if rsi_oversold_bars > 0 and rsi_raw > 0:
        if rsi_oversold_bars >= 36:  # 3時間以上
            duration_damping = 0.25
        elif rsi_oversold_bars >= 12:  # 1時間以上
            # 12bars→0.5, 36bars→0.25 の線形補間
            duration_damping = 0.5 - (rsi_oversold_bars - 12) / (36 - 12) * 0.25
        else:
            duration_damping = 1.0
        rsi_raw *= duration_damping
        print(f"RSI oversold for {rsi_oversold_bars} bars, damping={duration_damping:.2f}")
    
    if rsi_overbought_bars > 0 and rsi_raw < 0:
        if rsi_overbought_bars >= 36:
            duration_damping = 0.25
        elif rsi_overbought_bars >= 12:
            duration_damping = 0.5 - (rsi_overbought_bars - 12) / (36 - 12) * 0.25
        else:
            duration_damping = 1.0
        rsi_raw *= duration_damping
        print(f"RSI overbought for {rsi_overbought_bars} bars, damping={duration_damping:.2f}")
    
    # 改善2: トレンド相場でのRSI逆張り抑制
    # トレンド中にMACDが下向き(MACD<signal)なのにRSIが「買い」を出す場合は信用しない
    if regime == 'trending':
        macd_bearish = macd < signal
        macd_bullish = macd > signal
        if macd_bearish and rsi_raw > 0:
            # 下降トレンド中の「売られすぎ買い」→ 落ちるナイフ → 大幅抑制
            rsi_raw *= 0.3
            print(f"RSI buy suppressed in bearish trend: {rsi_raw:.3f}")
        elif macd_bullish and rsi_raw < 0:
            # 上昇トレンド中の「買われすぎ売り」→ FOMO継続の可能性 → 抑制
            rsi_raw *= 0.3
            print(f"RSI sell suppressed in bullish trend: {rsi_raw:.3f}")
    
    score += rsi_raw
    
    # MACD (ヒストグラムの大きさと勾配でグラデーション評価)
    histogram_val = macd - signal
    if current_price > 0:
        # ヒストグラムを価格比で正規化 (ATR%相当のスケール)
        norm_hist = histogram_val / current_price * 100  # % of price
        # ±0.1% で ±1.0 にスケール (5分足のMACD histogram典型値)
        hist_score = max(-1.0, min(1.0, norm_hist / 0.1))
        score += hist_score * w_macd
    else:
        # フォールバック: バイナリ
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
    if bb_upper != bb_lower:
        bb_position = (current_price - bb_lower) / (bb_upper - bb_lower)
        # 0.0=下限 → +w_bb, 0.5=中央 → 0, 1.0=上限 → -w_bb
        bb_score = (0.5 - bb_position) * 2 * w_bb
        # 極端な位置でボーナス（バンド外）
        if bb_position < 0.0 or bb_position > 1.0:
            bb_score *= 1.2
        score += max(-w_bb * 1.2, min(w_bb * 1.2, bb_score))
    
    return max(-1, min(1, score))


def calculate_atr(prices: list, period: int = 14, highs: list = None, lows: list = None) -> float:
    """
    ATR (Average True Range) 計算
    
    OHLC がある場合: TR = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    Close のみの場合: TR ≈ |Close[i] - Close[i-1]| (従来の近似)
    """
    if len(prices) < period + 1:
        return 0.0
    
    use_ohlc = (highs is not None and lows is not None
                and len(highs) == len(prices) and len(lows) == len(prices))
    
    true_ranges = []
    for i in range(1, len(prices)):
        if use_ohlc:
            # 正式な True Range
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - prices[i - 1])
            lc = abs(lows[i] - prices[i - 1])
            true_ranges.append(max(hl, hc, lc))
        else:
            true_ranges.append(abs(prices[i] - prices[i - 1]))
    
    # Wilder's smoothed average (exponential)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    
    return atr


def calculate_adx(prices: list, period: int = 14, highs: list = None, lows: list = None) -> float:
    """
    ADX (Average Directional Index) 計算
    
    トレンド強度の指標 (0-100):
    - ADX > 25: 強いトレンド相場
    - ADX < 20: レンジ相場
    - 20-25: 転換期
    
    OHLC がある場合:
    - +DM = max(High[i]-High[i-1], 0)  (上昇幅 > 下降幅の場合)
    - -DM = max(Low[i-1]-Low[i], 0)    (下降幅 > 上昇幅の場合)
    - TR  = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    Close のみの場合: 従来の近似
    """
    if len(prices) < period * 2 + 1:
        return 20.0  # データ不足時はneutral
    
    use_ohlc = (highs is not None and lows is not None
                and len(highs) == len(prices) and len(lows) == len(prices))
    
    # +DM, -DM 計算
    plus_dm = []
    minus_dm = []
    tr_list = []
    
    for i in range(1, len(prices)):
        if use_ohlc:
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            # True Range (正式)
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - prices[i - 1])
            lc = abs(lows[i] - prices[i - 1])
            tr = max(hl, hc, lc)
        else:
            up_move = prices[i] - prices[i - 1]
            down_move = prices[i - 1] - prices[i]
            tr = abs(prices[i] - prices[i - 1])
        
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


def calculate_macd_histogram_slope(prices: list, fast: int = 12, slow: int = 26,
                                    signal_period: int = 9, lookback: int = 3) -> float:
    """
    MACDヒストグラムの傾き（モメンタム変化率）を計算
    
    ヒストグラムが正から縮小 → モメンタム減速（反転の前兆）
    ヒストグラムが負から拡大 → 下降モメンタム加速
    
    Returns:
        正: モメンタム加速（上昇方向）
        負: モメンタム減速（下降方向）
        範囲: 概ね -1.0 ~ +1.0
    """
    if len(prices) < slow + signal_period + lookback:
        return 0.0
    
    # MACD系列全体を計算
    macd_series = calculate_macd_series(prices, fast, slow)
    if len(macd_series) < signal_period + lookback:
        return 0.0
    
    # シグナルラインのEMA系列を算出
    signal_series = []
    if len(macd_series) >= signal_period:
        multiplier = 2 / (signal_period + 1)
        ema = sum(macd_series[:signal_period]) / signal_period
        signal_series.append(ema)
        for val in macd_series[signal_period:]:
            ema = (val - ema) * multiplier + ema
            signal_series.append(ema)
    
    if len(signal_series) < lookback + 1:
        return 0.0
    
    # ヒストグラム系列の直近 lookback+1 本
    # macd_series と signal_series のアライメント
    offset = len(macd_series) - len(signal_series)
    hist_series = []
    for i in range(len(signal_series)):
        hist_series.append(macd_series[offset + i] - signal_series[i])
    
    if len(hist_series) < lookback + 1:
        return 0.0
    
    # 直近 lookback 本のヒストグラム変化の平均
    recent = hist_series[-(lookback + 1):]
    changes = [recent[i+1] - recent[i] for i in range(len(recent) - 1)]
    avg_change = sum(changes) / len(changes)
    
    # 価格で正規化してスケーリング
    current_price = prices[-1]
    if current_price <= 0:
        return 0.0
    
    # 正規化: ヒストグラム変化を価格比で表現し、±0.05%で±1.0にスケール
    norm_change = avg_change / current_price * 100
    slope = max(-1.0, min(1.0, norm_change / 0.05))
    
    return slope


def calculate_volume_signal(volumes: list, period: int = 20) -> float:
    """
    出来高シグナル計算
    
    直近出来高が移動平均に対してどれだけ乖離しているかを返す。
    戻り値は乗数 (multiplier):
    - 1.0: 平均的な出来高 → シグナルに影響なし
    - >1.0: 出来高急増 → トレンド確信度を強化
    - 最大 1.3 にキャップ (過度な増幅を防止)
    
    出来高が平均以下の場合は 1.0 (減衰させない — 偽シグナル抑制は他で対応)
    """
    if not volumes or len(volumes) < period + 1:
        return 1.0
    
    # 直近を除いた period 本分の平均出来高
    recent_volumes = volumes[-(period + 1):-1]
    avg_volume = sum(recent_volumes) / len(recent_volumes)
    
    if avg_volume <= 0:
        return 1.0
    
    current_volume = volumes[-1]
    ratio = current_volume / avg_volume
    
    # 平均の 1.5 倍以上で増幅開始、2.5倍で上限 1.3
    if ratio <= 1.5:
        return 1.0
    
    # 線形補間: ratio 1.5→1.0, ratio 2.5→1.3
    multiplier = 1.0 + (ratio - 1.5) / (2.5 - 1.5) * 0.3
    return min(1.3, multiplier)
