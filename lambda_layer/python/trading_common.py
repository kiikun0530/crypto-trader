# =============================================================================
# Shared Trading Utilities
# =============================================================================
# 全Lambda関数で共有するユーティリティ関数・設定
# lambda_layer/python/ に配置 → Layer経由で全Lambdaからimport可能
# =============================================================================

import json
import os
import urllib.request
from decimal import Decimal

import boto3

# -----------------------------------------------------------------------------
# DynamoDB
# -----------------------------------------------------------------------------
dynamodb = boto3.resource('dynamodb')

# -----------------------------------------------------------------------------
# テーブル名設定
# -----------------------------------------------------------------------------
PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')
POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')  # crypto-order管理、aggregator読取用
SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
SENTIMENT_TABLE = os.environ.get('SENTIMENT_TABLE', 'eth-trading-sentiment')
ANALYSIS_STATE_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')
TF_SCORES_TABLE = os.environ.get('TF_SCORES_TABLE', 'eth-trading-tf-scores')

# -----------------------------------------------------------------------------
# 通貨ペア設定 (TRADING_PAIRS_CONFIG 環境変数から読み込み)
# Phase 5: 6通貨 → 3通貨 (BTC, ETH, XRP) に集中
# -----------------------------------------------------------------------------
DEFAULT_PAIRS = {
    "btc_usdt": {
        "binance": "BTCUSDT",
        "coincheck": "btc_jpy",
        "news": "BTC",
        "name": "Bitcoin"
    },
    "eth_usdt": {
        "binance": "ETHUSDT",
        "coincheck": "eth_jpy",
        "news": "ETH",
        "name": "Ethereum"
    },
    "xrp_usdt": {
        "binance": "XRPUSDT",
        "coincheck": "xrp_jpy",
        "news": "XRP",
        "name": "XRP"
    }
}
TRADING_PAIRS = json.loads(
    os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS))
)

# -----------------------------------------------------------------------------
# マルチタイムフレーム設定
# 各TFの Binance interval, TTL, Chronos入力長, スコアスケールを定義
# -----------------------------------------------------------------------------
TIMEFRAME_CONFIG = {
    "15m": {
        "binance_interval": "15m",
        "ttl_days": 14,
        "chronos_input_length": 336,    # 336 × 15min = 3.5 days
        "chronos_prediction_length": 12, # 12 × 15min = 3 hours ahead
        "score_scale_percent": 3.0,      # ±3% for 3h prediction
        "warmup_candles": 1344,          # 14 days of 15m data
        "description": "15-minute candles",
        # TF別テクニカル分析パラメータ
        "rsi_decay_mild_bars": 4,        # 4本 = 1h相当 → 50%減衰
        "rsi_decay_strong_bars": 12,     # 12本 = 3h相当 → 25%減衰
        "macd_hist_scale": 0.10,         # ±0.10% of price for ±1.0 score
        "sma_divergence_scale": 1.5,     # ±1.5% SMA20-200乖離でfull score
        "bb_baseline": 0.030,            # BB幅の基準値 3.0%
    },
    "1h": {
        "binance_interval": "1h",
        "ttl_days": 30,
        "chronos_input_length": 336,    # 336 × 1h = 14 days
        "chronos_prediction_length": 12, # 12 × 1h = 12 hours ahead
        "score_scale_percent": 5.0,      # ±5% for 12h prediction
        "warmup_candles": 720,           # 30 days of 1h data
        "description": "1-hour candles",
        "rsi_decay_mild_bars": 3,        # 3本 = 3h相当
        "rsi_decay_strong_bars": 6,      # 6本 = 6h相当
        "macd_hist_scale": 0.20,         # ±0.20%
        "sma_divergence_scale": 3.0,     # ±3.0%
        "bb_baseline": 0.045,            # 4.5%
    },
    "4h": {
        "binance_interval": "4h",
        "ttl_days": 90,
        "chronos_input_length": 336,    # 336 × 4h = 56 days
        "chronos_prediction_length": 12, # 12 × 4h = 48 hours ahead
        "score_scale_percent": 10.0,     # ±10% for 48h prediction
        "warmup_candles": 540,           # 90 days of 4h data
        "description": "4-hour candles",
        "rsi_decay_mild_bars": 2,        # 2本 = 8h相当
        "rsi_decay_strong_bars": 4,      # 4本 = 16h相当
        "macd_hist_scale": 0.50,         # ±0.50%
        "sma_divergence_scale": 5.0,     # ±5.0%
        "bb_baseline": 0.070,            # 7.0%
    },
    "1d": {
        "binance_interval": "1d",
        "ttl_days": 365,
        "chronos_input_length": 250,    # 250 × 1d = 250 days
        "chronos_prediction_length": 12, # 12 × 1d = 12 days ahead
        "score_scale_percent": 20.0,     # ±20% for 12d prediction
        "warmup_candles": 250,           # 250 days of 1d data
        "description": "Daily candles",
        "rsi_decay_mild_bars": 2,        # 2本 = 2日相当
        "rsi_decay_strong_bars": 3,      # 3本 = 3日相当
        "macd_hist_scale": 1.00,         # ±1.00%
        "sma_divergence_scale": 10.0,    # ±10.0%
        "bb_baseline": 0.120,            # 12.0%
    },
}

# 有効なタイムフレーム一覧
ACTIVE_TIMEFRAMES = list(TIMEFRAME_CONFIG.keys())

# マルチTF統合ウェイト（meta-aggregator用）
TIMEFRAME_WEIGHTS = {
    "15m": 0.20,  # エントリータイミング
    "1h":  0.35,  # 中期トレンド方向（最重要）
    "4h":  0.30,  # 大局観
    "1d":  0.15,  # 長期トレンド確認
}


def make_pair_tf_key(pair: str, timeframe: str) -> str:
    """DynamoDB用の pair#timeframe キーを生成"""
    return f"{pair}#{timeframe}"


def get_ttl_seconds(timeframe: str) -> int:
    """タイムフレームに応じたTTL（秒）を返す"""
    days = TIMEFRAME_CONFIG.get(timeframe, {}).get('ttl_days', 14)
    return days * 86400

# -----------------------------------------------------------------------------
# Slack通知
# -----------------------------------------------------------------------------
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')


def send_slack_notification(message: str, blocks: list = None) -> bool:
    """Slack Webhook通知を送信"""
    if not SLACK_WEBHOOK_URL:
        return False
    try:
        if blocks:
            payload = {"blocks": blocks}
        else:
            payload = {
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": message}
                    }
                ]
            }
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# 価格取得 (Coincheck)
# -----------------------------------------------------------------------------
def get_current_price(pair: str) -> float:
    """Coincheck APIから現在価格を取得"""
    url = f"https://coincheck.com/api/ticker?pair={pair}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())
        return float(data['last'])


# -----------------------------------------------------------------------------
# ポジション取得
# -----------------------------------------------------------------------------
def get_active_position(pair: str, table_name: str = None) -> dict:
    """アクティブポジション（未クローズ）を取得"""
    table = dynamodb.Table(table_name or POSITIONS_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=10
    )
    items = response.get('Items', [])
    for item in items:
        if not item.get('closed'):
            return item
    return None


def find_all_active_positions(table_name: str = None) -> list:
    """全通貨ペアのアクティブポジションを取得"""
    positions = []
    for pair, config in TRADING_PAIRS.items():
        coincheck_pair = config.get('coincheck', pair)
        position = get_active_position(coincheck_pair, table_name)
        if position:
            positions.append({
                'pair': pair,
                'coincheck_pair': coincheck_pair,
                'position': position
            })
    return positions


# -----------------------------------------------------------------------------
# ユーティリティ
# -----------------------------------------------------------------------------
