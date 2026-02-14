"""
結果判定 Lambda (result-checker)

統合判定シグナル (BUY/SELL) の発行後、複数の時間窓で価格変動を記録。
signals テーブルの既存レコードに結果フィールドを追記する。

時間窓: 1h, 4h, 12h, 3d
的中判定: 方向一致 + 閾値超え → WIN / LOSS / DRAW

実行: EventBridge 15分間隔
"""
import json
import os
import time
import traceback
import boto3
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from trading_common import TRADING_PAIRS, dynamodb

SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
PRICES_TABLE = os.environ.get('PRICES_TABLE', 'eth-trading-prices')

# 時間窓設定 (秒)
RESULT_WINDOWS = {
    'result_1h':  3600,       # 1時間後
    'result_4h':  14400,      # 4時間後
    'result_12h': 43200,      # 12時間後
    'result_3d':  259200,     # 3日後
}

# 的中判定閾値 (%)
WIN_THRESHOLD_PCT = float(os.environ.get('WIN_THRESHOLD_PCT', '0.3'))

# 検索対象期間 (最大窓 3d + 2h バッファ)
LOOKBACK_SECONDS = 259200 + 7200


def handler(event, context):
    """メインハンドラ: 全通貨のBUY/SELLシグナルの結果を判定"""
    try:
        now = int(time.time())
        signals_table = dynamodb.Table(SIGNALS_TABLE)

        total_updated = 0
        total_checked = 0
        total_completed = 0

        for pair in TRADING_PAIRS:
            # 直近 3d+2h 分のシグナルを取得
            since = now - LOOKBACK_SECONDS
            response = signals_table.query(
                KeyConditionExpression=Key('pair').eq(pair) & Key('timestamp').gte(since),
                ScanIndexForward=False,
            )
            items = response.get('Items', [])

            for item in items:
                signal = item.get('signal', 'HOLD')
                if signal == 'HOLD':
                    continue

                total_checked += 1
                updated, completed = _check_signal_results(item, pair, now)
                if updated:
                    total_updated += 1
                if completed:
                    total_completed += 1

        print(f"[result-checker] Done: checked={total_checked}, "
              f"updated={total_updated}, fully_completed={total_completed}")

        return {
            'statusCode': 200,
            'checked': total_checked,
            'updated': total_updated,
            'completed': total_completed,
        }

    except Exception as e:
        print(f"[result-checker] Error: {e}")
        traceback.print_exc()
        return {'statusCode': 500, 'error': str(e)}


# =============================================================================
# 結果判定ロジック
# =============================================================================

def _check_signal_results(item: dict, pair: str, now: int) -> tuple:
    """
    1つのシグナルレコードに対して、未記入の時間窓を埋める。

    Returns:
        (updated: bool, all_completed: bool)
    """
    signal = item['signal']  # BUY or SELL
    signal_ts = int(item['timestamp'])

    # entry_price がまだ保存されていなければ prices テーブルから取得
    entry_price = float(item.get('entry_price', 0))
    if entry_price <= 0:
        entry_price = _get_price_at(pair, signal_ts)
        if entry_price <= 0:
            print(f"[result-checker] No entry_price for {pair}@{signal_ts}, skip")
            return False, False

    updates = {}
    pending_windows = 0

    for window_key, window_seconds in RESULT_WINDOWS.items():
        # 既に記入済みならスキップ
        if item.get(window_key):
            continue

        target_ts = signal_ts + window_seconds
        if now < target_ts:
            # まだ時間が来ていない
            pending_windows += 1
            continue

        # 観察時点の価格を取得
        exit_price = _get_price_at(pair, target_ts)
        if exit_price <= 0:
            pending_windows += 1
            continue

        # 期間中の最有利/最不利を取得
        max_fav, max_adv = _get_extremes(pair, signal_ts, target_ts, signal)

        # 変動率計算
        pct = (exit_price - entry_price) / entry_price * 100

        # 最有利/最不利変動率
        fav_pct = ((max_fav - entry_price) / entry_price * 100) if max_fav > 0 else 0.0
        adv_pct = ((max_adv - entry_price) / entry_price * 100) if max_adv > 0 else 0.0

        # 的中判定
        if signal == 'BUY':
            outcome = ('WIN' if pct > WIN_THRESHOLD_PCT
                       else 'LOSS' if pct < -WIN_THRESHOLD_PCT
                       else 'DRAW')
        else:  # SELL
            outcome = ('WIN' if pct < -WIN_THRESHOLD_PCT
                       else 'LOSS' if pct > WIN_THRESHOLD_PCT
                       else 'DRAW')

        updates[window_key] = {
            'price_change_pct': round(pct, 3),
            'outcome': outcome,
            'exit_price': round(exit_price, 8),
            'max_favorable_pct': round(fav_pct, 3),
            'max_adverse_pct': round(adv_pct, 3),
        }

    if not updates:
        return False, (pending_windows == 0)

    _update_signal(pair, signal_ts, entry_price, updates)
    all_completed = (pending_windows == 0)
    return True, all_completed


# =============================================================================
# 価格取得ヘルパー
# =============================================================================

def _get_price_at(pair: str, target_ts: int) -> float:
    """指定時刻に最も近い価格を prices テーブルから取得。
    15m 足 → 1h 足の順にフォールバック。"""
    table = dynamodb.Table(PRICES_TABLE)

    for tf in ['15m', '1h']:
        pair_tf_key = f"{pair}#{tf}"
        try:
            result = table.query(
                KeyConditionExpression=(
                    Key('pair').eq(pair_tf_key) & Key('timestamp').lte(target_ts)
                ),
                ScanIndexForward=False,
                Limit=1,
            )
            items = result.get('Items', [])
            if items:
                return float(items[0].get('close', 0))
        except Exception as e:
            print(f"[result-checker] Price lookup error {pair}#{tf}@{target_ts}: {e}")

    return 0.0


def _get_extremes(pair: str, start_ts: int, end_ts: int, signal: str) -> tuple:
    """
    期間中の最有利/最不利価格を取得 (1h 足ベース)。

    Returns:
        (max_favorable, max_adverse)
        BUY: favorable=期間中最高値, adverse=期間中最安値
        SELL: favorable=期間中最安値, adverse=期間中最高値
    """
    table = dynamodb.Table(PRICES_TABLE)
    pair_tf_key = f"{pair}#1h"

    try:
        items = []
        last_key = None
        while True:
            kwargs = dict(
                KeyConditionExpression=(
                    Key('pair').eq(pair_tf_key)
                    & Key('timestamp').between(start_ts, end_ts)
                ),
                ScanIndexForward=True,
            )
            if last_key:
                kwargs['ExclusiveStartKey'] = last_key
            result = table.query(**kwargs)
            items.extend(result.get('Items', []))
            last_key = result.get('LastEvaluatedKey')
            if not last_key:
                break

        if not items:
            return 0.0, 0.0

        highs = [float(i.get('high', i.get('close', 0))) for i in items]
        lows = [float(i.get('low', i.get('close', 0))) for i in items]

        if signal == 'BUY':
            return max(highs), min(lows)
        else:
            return min(lows), max(highs)

    except Exception as e:
        print(f"[result-checker] Extremes error {pair} {start_ts}-{end_ts}: {e}")
        return 0.0, 0.0


# =============================================================================
# DynamoDB 更新
# =============================================================================

def _update_signal(pair: str, timestamp: int, entry_price: float,
                   updates: dict):
    """signals テーブルのレコードに結果フィールドを追記"""
    table = dynamodb.Table(SIGNALS_TABLE)

    try:
        expr_parts = []
        expr_names = {}
        expr_values = {}

        # entry_price を保存 (初回のみ — 既存レコードに追記)
        expr_parts.append('#ep = :ep')
        expr_names['#ep'] = 'entry_price'
        expr_values[':ep'] = _to_decimal(entry_price)

        for window_key, result_data in updates.items():
            alias = window_key.replace('_', '')
            expr_parts.append(f'#{alias} = :{alias}')
            expr_names[f'#{alias}'] = window_key
            expr_values[f':{alias}'] = {
                k: _to_decimal(v) if isinstance(v, float) else v
                for k, v in result_data.items()
            }

        update_expr = 'SET ' + ', '.join(expr_parts)

        table.update_item(
            Key={'pair': pair, 'timestamp': timestamp},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )

        windows_str = ', '.join(updates.keys())
        print(f"[result-checker] Updated {pair}@{timestamp}: {windows_str}")

    except Exception as e:
        print(f"[result-checker] Update error {pair}@{timestamp}: {e}")


def _to_decimal(value) -> Decimal:
    """float → DynamoDB Decimal"""
    if isinstance(value, float):
        return Decimal(str(round(value, 8)))
    if isinstance(value, int):
        return Decimal(str(value))
    return Decimal(str(value))
