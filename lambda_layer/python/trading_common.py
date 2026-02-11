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
POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
TRADES_TABLE = os.environ.get('TRADES_TABLE', 'eth-trading-trades')
SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
SENTIMENT_TABLE = os.environ.get('SENTIMENT_TABLE', 'eth-trading-sentiment')

# -----------------------------------------------------------------------------
# 通貨ペア設定 (TRADING_PAIRS_CONFIG 環境変数から読み込み)
# -----------------------------------------------------------------------------
DEFAULT_PAIRS = {
    "eth_usdt": {
        "binance": "ETHUSDT",
        "coincheck": "eth_jpy",
        "news": "ETH",
        "name": "Ethereum"
    }
}
TRADING_PAIRS = json.loads(
    os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS))
)

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
def get_currency_from_pair(pair: str) -> str:
    """通貨ペアから通貨コードを抽出 (例: 'eth_jpy' -> 'eth')"""
    return pair.split('_')[0]
