"""
Signal API Lambda
公開シグナルAPIエンドポイント

機能:
- 無料ユーザー: 30分遅延のシグナルデータを提供
- 有料ユーザー: リアルタイムシグナルデータを提供 (APIキー認証)
- 通貨ランキング、スコア推移、パフォーマンス統計を公開

エンドポイント:
- GET /signals/latest      → 最新シグナル（無料=30分遅延, 有料=リアルタイム）
- GET /signals/history     → シグナル履歴（直近24h, 無料=30分遅延）
- GET /signals/performance → 取引パフォーマンス統計
- GET /signals/ranking     → 通貨ランキング
"""
import json
import os
import time
import boto3
from decimal import Decimal
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')

SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
TRADES_TABLE = os.environ.get('TRADES_TABLE', 'eth-trading-trades')
POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
MARKET_CONTEXT_TABLE = os.environ.get('MARKET_CONTEXT_TABLE', 'eth-trading-market-context')

# 無料ユーザー向け遅延時間（秒）
FREE_DELAY_SECONDS = int(os.environ.get('FREE_DELAY_SECONDS', '1800'))  # 30分

# 有料APIキーのテーブル (DynamoDB)
API_KEYS_TABLE = os.environ.get('API_KEYS_TABLE', 'eth-trading-api-keys')

# 通貨ペア設定
DEFAULT_PAIRS = {
    "eth_usdt": {"name": "Ethereum", "symbol": "ETH"},
    "btc_usdt": {"name": "Bitcoin", "symbol": "BTC"},
    "xrp_usdt": {"name": "XRP", "symbol": "XRP"},
    "sol_usdt": {"name": "Solana", "symbol": "SOL"},
    "doge_usdt": {"name": "Dogecoin", "symbol": "DOGE"},
    "avax_usdt": {"name": "Avalanche", "symbol": "AVAX"},
}
TRADING_PAIRS = json.loads(
    os.environ.get('TRADING_PAIRS_CONFIG', json.dumps(DEFAULT_PAIRS))
)

# CORS設定
CORS_ORIGIN = os.environ.get('CORS_ORIGIN', '*')


class DecimalEncoder(json.JSONEncoder):
    """DynamoDB Decimal型のJSON変換"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def handler(event, context):
    """API Gateway Lambda Proxy統合ハンドラ"""
    try:
        http_method = event.get('httpMethod', 'GET')
        path = event.get('path', '/')
        query_params = event.get('queryStringParameters') or {}
        headers = event.get('headers') or {}

        # APIキー認証チェック
        is_premium = check_premium(headers)

        # ルーティング
        if path == '/signals/latest':
            body = get_latest_signals(query_params, is_premium)
        elif path == '/signals/history':
            body = get_signal_history(query_params, is_premium)
        elif path == '/signals/performance':
            body = get_performance_stats(query_params)
        elif path == '/signals/ranking':
            body = get_ranking(query_params, is_premium)
        elif path == '/signals/market':
            body = get_market_context()
        elif path == '/signals/health':
            body = {'status': 'ok', 'timestamp': int(time.time())}
        else:
            return response(404, {'error': 'Not found'})

        return response(200, body)

    except Exception as e:
        print(f"API Error: {e}")
        import traceback
        traceback.print_exc()
        return response(500, {'error': 'Internal server error'})


def check_premium(headers: dict) -> bool:
    """APIキーで有料ユーザーかチェック"""
    api_key = headers.get('x-api-key') or headers.get('X-Api-Key', '')
    if not api_key:
        return False

    try:
        table = dynamodb.Table(API_KEYS_TABLE)
        result = table.get_item(Key={'api_key': api_key})
        item = result.get('Item')
        if not item:
            return False
        # 有効期限チェック
        expires_at = int(item.get('expires_at', 0))
        if expires_at > 0 and expires_at < int(time.time()):
            return False
        return item.get('is_active', False)
    except Exception as e:
        print(f"API key check error: {e}")
        return False


def get_latest_signals(params: dict, is_premium: bool) -> dict:
    """最新シグナルを取得（無料=30分遅延）"""
    table = dynamodb.Table(SIGNALS_TABLE)
    now = int(time.time())

    # 無料ユーザーは遅延タイムスタンプまでのデータのみ
    cutoff_time = now if is_premium else (now - FREE_DELAY_SECONDS)

    signals = []
    for pair in TRADING_PAIRS:
        result = table.query(
            KeyConditionExpression=Key('pair').eq(pair) & Key('timestamp').lte(cutoff_time),
            ScanIndexForward=False,
            Limit=1
        )
        items = result.get('Items', [])
        if items:
            item = items[0]
            pair_info = TRADING_PAIRS.get(pair, {})
            signals.append({
                'pair': pair,
                'name': pair_info.get('name', pair),
                'symbol': pair_info.get('news', pair_info.get('symbol', pair.split('_')[0].upper())),
                'signal': item.get('signal', 'HOLD'),
                'score': item.get('score'),
                'technical_score': item.get('technical_score'),
                'chronos_score': item.get('chronos_score'),
                'sentiment_score': item.get('sentiment_score'),
                'market_context_score': item.get('market_context_score'),
                'buy_threshold': item.get('buy_threshold'),
                'sell_threshold': item.get('sell_threshold'),
                'timestamp': item.get('timestamp'),
            })

    # スコア順にソート
    signals.sort(key=lambda x: float(x.get('score', 0) or 0), reverse=True)

    return {
        'signals': signals,
        'is_realtime': is_premium,
        'delay_minutes': 0 if is_premium else FREE_DELAY_SECONDS // 60,
        'timestamp': now,
        'next_update': '~5 minutes (analysis runs every 5 min)',
    }


def get_signal_history(params: dict, is_premium: bool) -> dict:
    """シグナル履歴（直近24h）"""
    table = dynamodb.Table(SIGNALS_TABLE)
    now = int(time.time())

    pair = params.get('pair', 'eth_usdt')
    hours = min(int(params.get('hours', '24')), 72)  # 最大72時間
    limit = min(int(params.get('limit', '100')), 500)

    since = now - (hours * 3600)
    cutoff_time = now if is_premium else (now - FREE_DELAY_SECONDS)

    result = table.query(
        KeyConditionExpression=Key('pair').eq(pair) & Key('timestamp').between(since, cutoff_time),
        ScanIndexForward=False,
        Limit=limit
    )

    items = result.get('Items', [])
    history = []
    for item in items:
        history.append({
            'signal': item.get('signal', 'HOLD'),
            'score': item.get('score'),
            'technical_score': item.get('technical_score'),
            'chronos_score': item.get('chronos_score'),
            'sentiment_score': item.get('sentiment_score'),
            'market_context_score': item.get('market_context_score'),
            'buy_threshold': item.get('buy_threshold'),
            'sell_threshold': item.get('sell_threshold'),
            'bb_width': item.get('bb_width'),
            'timestamp': item.get('timestamp'),
        })

    pair_info = TRADING_PAIRS.get(pair, {})
    return {
        'pair': pair,
        'name': pair_info.get('name', pair),
        'history': history,
        'count': len(history),
        'hours': hours,
        'is_realtime': is_premium,
    }


def get_performance_stats(params: dict) -> dict:
    """取引パフォーマンス統計（公開情報）"""
    table = dynamodb.Table(TRADES_TABLE)
    now = int(time.time())
    days = min(int(params.get('days', '30')), 90)
    since = now - (days * 86400)

    stats = {
        'period_days': days,
        'pairs': {},
        'total': {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl_pct': 0,
        }
    }

    for pair in TRADING_PAIRS:
        result = table.query(
            KeyConditionExpression=Key('pair').eq(pair) & Key('timestamp').gte(since),
            ScanIndexForward=False,
            Limit=200
        )
        items = result.get('Items', [])

        pair_wins = 0
        pair_losses = 0
        pair_pnl_pct = 0

        for item in items:
            pnl_pct = float(item.get('pnl_pct', 0) or 0)
            if item.get('action') == 'SELL' or item.get('side') == 'sell':
                if pnl_pct > 0:
                    pair_wins += 1
                elif pnl_pct < 0:
                    pair_losses += 1
                pair_pnl_pct += pnl_pct

        total_trades = pair_wins + pair_losses
        pair_info = TRADING_PAIRS.get(pair, {})
        if total_trades > 0:
            stats['pairs'][pair] = {
                'name': pair_info.get('name', pair),
                'trades': total_trades,
                'wins': pair_wins,
                'losses': pair_losses,
                'win_rate': round(pair_wins / total_trades * 100, 1) if total_trades > 0 else 0,
                'total_pnl_pct': round(pair_pnl_pct, 2),
            }

        stats['total']['trades'] += total_trades
        stats['total']['wins'] += pair_wins
        stats['total']['losses'] += pair_losses
        stats['total']['total_pnl_pct'] += pair_pnl_pct

    total = stats['total']
    total['total_pnl_pct'] = round(total['total_pnl_pct'], 2)
    total['win_rate'] = round(
        total['wins'] / total['trades'] * 100, 1
    ) if total['trades'] > 0 else 0

    return stats


def get_ranking(params: dict, is_premium: bool) -> dict:
    """通貨ランキング（最新スコア順）"""
    table = dynamodb.Table(SIGNALS_TABLE)
    now = int(time.time())
    cutoff_time = now if is_premium else (now - FREE_DELAY_SECONDS)

    ranking = []
    for pair in TRADING_PAIRS:
        result = table.query(
            KeyConditionExpression=Key('pair').eq(pair) & Key('timestamp').lte(cutoff_time),
            ScanIndexForward=False,
            Limit=1
        )
        items = result.get('Items', [])
        if items:
            item = items[0]
            pair_info = TRADING_PAIRS.get(pair, {})
            ranking.append({
                'rank': 0,
                'pair': pair,
                'name': pair_info.get('name', pair),
                'symbol': pair_info.get('news', pair_info.get('symbol', pair.split('_')[0].upper())),
                'score': item.get('score'),
                'signal': item.get('signal', 'HOLD'),
                'timestamp': item.get('timestamp'),
            })

    # スコア順にソート
    ranking.sort(key=lambda x: float(x.get('score', 0) or 0), reverse=True)
    for i, r in enumerate(ranking):
        r['rank'] = i + 1

    return {
        'ranking': ranking,
        'is_realtime': is_premium,
        'timestamp': now,
    }


def get_market_context() -> dict:
    """マーケットコンテキスト（全員に公開）"""
    table = dynamodb.Table(MARKET_CONTEXT_TABLE)
    now = int(time.time())
    since = now - 3600  # 直近1時間

    context = {}
    for ctx_type in ['fear_greed', 'funding_rate', 'btc_dominance']:
        result = table.query(
            KeyConditionExpression=Key('context_type').eq(ctx_type) & Key('timestamp').gte(since),
            ScanIndexForward=False,
            Limit=1
        )
        items = result.get('Items', [])
        if items:
            item = items[0]
            context[ctx_type] = {
                'value': item.get('value'),
                'classification': item.get('classification', ''),
                'timestamp': item.get('timestamp'),
            }

    return {
        'market_context': context,
        'timestamp': now,
    }


def response(status_code: int, body: dict) -> dict:
    """API Gateway レスポンス生成"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': CORS_ORIGIN,
            'Access-Control-Allow-Headers': 'Content-Type,X-Api-Key',
            'Access-Control-Allow-Methods': 'GET,OPTIONS',
            'Cache-Control': 'public, max-age=60',
        },
        'body': json.dumps(body, cls=DecimalEncoder, ensure_ascii=False)
    }
