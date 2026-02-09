"""
Daily Reporter Lambda
毎日 23:00 JST に実行。1日の取引・シグナル・市場データを集計し、
① S3にJSON保存 ② Slackサマリ通知 ③ GitHub Actions改善ワークフロートリガー

データソース:
- trades: 直近24h/7d/30dの取引履歴
- signals: 直近24hのシグナル統計
- positions: アクティブポジション
- market-context: 直近の市場環境
- improvements: 直近の自動改善履歴
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from collections import defaultdict

import boto3

# === 環境変数 ===
TRADES_TABLE = os.environ.get('TRADES_TABLE', 'eth-trading-trades')
SIGNALS_TABLE = os.environ.get('SIGNALS_TABLE', 'eth-trading-signals')
POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
MARKET_CONTEXT_TABLE = os.environ.get('MARKET_CONTEXT_TABLE', 'eth-trading-market-context')
IMPROVEMENTS_TABLE = os.environ.get('IMPROVEMENTS_TABLE', 'eth-trading-improvements')
REPORT_BUCKET = os.environ.get('REPORT_BUCKET', 'eth-trading-daily-reports')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
GITHUB_TOKEN_SECRET_ARN = os.environ.get('GITHUB_TOKEN_SECRET_ARN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')
TRADING_PAIRS_CONFIG = os.environ.get('TRADING_PAIRS_CONFIG', '{}')

dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1')
s3 = boto3.client('s3', region_name='ap-northeast-1')
secrets = boto3.client('secretsmanager', region_name='ap-northeast-1')

JST = timezone(timedelta(hours=9))


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def handler(event, context):
    """メインハンドラー: 日次レポート生成→S3保存→Slack→GitHub Actions"""
    try:
        now = int(time.time())
        today_jst = datetime.fromtimestamp(now, tz=JST).strftime('%Y-%m-%d')
        print(f"=== Daily Report for {today_jst} ===")

        # 通貨ペア設定を取得
        pairs_config = json.loads(TRADING_PAIRS_CONFIG) if TRADING_PAIRS_CONFIG else {}
        all_pairs = list(pairs_config.keys()) if pairs_config else [
            'eth_usdt', 'btc_usdt', 'xrp_usdt', 'sol_usdt', 'doge_usdt', 'avax_usdt'
        ]
        pair_to_coincheck = {}
        for k, v in pairs_config.items():
            if isinstance(v, dict):
                pair_to_coincheck[k] = v.get('coincheck', k)

        # データ収集
        trades_24h = fetch_trades(now - 86400, now, all_pairs, pair_to_coincheck)
        trades_7d = fetch_trades(now - 86400 * 7, now, all_pairs, pair_to_coincheck)
        trades_30d = fetch_trades(now - 86400 * 30, now, all_pairs, pair_to_coincheck)
        signals_24h = fetch_signals(now - 86400, now, all_pairs)
        active_positions = fetch_positions(all_pairs, pair_to_coincheck)
        market_context = fetch_market_context()
        recent_improvements = fetch_improvements(now - 86400 * 14)

        # 統計計算
        report = build_report(
            date=today_jst,
            timestamp=now,
            trades_24h=trades_24h,
            trades_7d=trades_7d,
            trades_30d=trades_30d,
            signals_24h=signals_24h,
            active_positions=active_positions,
            market_context=market_context,
            recent_improvements=recent_improvements
        )

        # S3保存
        s3_key = f"daily-reports/{today_jst}.json"
        save_to_s3(report, s3_key)
        print(f"Report saved to s3://{REPORT_BUCKET}/{s3_key}")

        # Slack通知
        send_slack_summary(report)

        # GitHub Actions トリガー (データ品質ゲート)
        dq = report.get('data_quality', {})
        if dq.get('allow_improvement', False):
            trigger_auto_improve(report)
            trigger_status = 'triggered'
        else:
            skip_reasons = dq.get('skip_reasons', ['unknown'])
            print(f"Auto-improve SKIPPED: {', '.join(skip_reasons)}")
            print(f"  confidence_score={dq.get('confidence_score', 0)}, "
                  f"7d_trades={dq.get('trades_7d_paired', 0)}, "
                  f"ci_width={dq.get('win_rate_7d_ci', {}).get('width', 'N/A')}")
            trigger_status = f'skipped({skip_reasons[0]})'

        return {
            'statusCode': 200,
            'body': json.dumps({
                'date': today_jst,
                'trades_24h': report['trades']['total'],
                'signals_24h': report['signals']['total'],
                's3_key': s3_key,
                'auto_improve': trigger_status
            })
        }

    except Exception as e:
        print(f"Error in daily-reporter: {str(e)}")
        import traceback
        traceback.print_exc()
        # エラーでもSlack通知
        try:
            notify_error(str(e))
        except Exception:
            pass
        return {'statusCode': 500, 'body': str(e)}


# =============================================================================
# データ取得
# =============================================================================

def fetch_trades(start_ts: int, end_ts: int, pairs: list, pair_map: dict) -> list:
    """全通貨の取引をスキャン"""
    table = dynamodb.Table(TRADES_TABLE)
    all_trades = []
    for pair in pairs:
        coincheck_pair = pair_map.get(pair, pair)
        try:
            response = table.query(
                KeyConditionExpression='pair = :p AND #ts BETWEEN :s AND :e',
                ExpressionAttributeNames={'#ts': 'timestamp'},
                ExpressionAttributeValues={
                    ':p': coincheck_pair,
                    ':s': start_ts,
                    ':e': end_ts
                }
            )
            for item in response.get('Items', []):
                item['_analysis_pair'] = pair
                all_trades.append(item)
        except Exception as e:
            print(f"Error querying trades for {coincheck_pair}: {e}")
    return sorted(all_trades, key=lambda x: float(x.get('timestamp', 0)))


def fetch_signals(start_ts: int, end_ts: int, pairs: list) -> list:
    """全通貨のシグナルをクエリ"""
    table = dynamodb.Table(SIGNALS_TABLE)
    all_signals = []
    for pair in pairs:
        try:
            response = table.query(
                KeyConditionExpression='pair = :p AND #ts BETWEEN :s AND :e',
                ExpressionAttributeNames={'#ts': 'timestamp'},
                ExpressionAttributeValues={
                    ':p': pair,
                    ':s': start_ts,
                    ':e': end_ts
                }
            )
            all_signals.extend(response.get('Items', []))
        except Exception as e:
            print(f"Error querying signals for {pair}: {e}")
    return all_signals


def fetch_positions(pairs: list, pair_map: dict) -> list:
    """アクティブポジション取得"""
    table = dynamodb.Table(POSITIONS_TABLE)
    active = []
    for pair in pairs:
        coincheck_pair = pair_map.get(pair, pair)
        try:
            response = table.query(
                KeyConditionExpression='pair = :p',
                FilterExpression='closed = :f',
                ExpressionAttributeValues={
                    ':p': coincheck_pair,
                    ':f': False
                }
            )
            active.extend(response.get('Items', []))
        except Exception as e:
            print(f"Error querying positions for {coincheck_pair}: {e}")
    return active


def fetch_market_context() -> dict:
    """最新のマーケットコンテキスト"""
    table = dynamodb.Table(MARKET_CONTEXT_TABLE)
    try:
        response = table.query(
            KeyConditionExpression='context_type = :ct',
            ExpressionAttributeValues={':ct': 'market_context'},
            ScanIndexForward=False,
            Limit=1
        )
        items = response.get('Items', [])
        return items[0] if items else {}
    except Exception as e:
        print(f"Error fetching market context: {e}")
        return {}


def fetch_improvements(since_ts: int) -> list:
    """直近の自動改善履歴"""
    table = dynamodb.Table(IMPROVEMENTS_TABLE)
    try:
        response = table.scan(
            FilterExpression='#ts > :s',
            ExpressionAttributeNames={'#ts': 'timestamp'},
            ExpressionAttributeValues={':s': since_ts}
        )
        return sorted(response.get('Items', []),
                       key=lambda x: float(x.get('timestamp', 0)),
                       reverse=True)
    except Exception as e:
        print(f"Error fetching improvements: {e}")
        return []


# =============================================================================
# レポート生成
# =============================================================================

def build_data_quality(trades_24h, trades_7d, trades_30d, signals_24h,
                      recent_improvements) -> dict:
    """
    データ品質を評価し、自動改善の可否を判定する。
    コードレベルの強制ゲート — Claudeプロンプト任せにしない。
    """
    import math

    paired_24h = pair_trades(trades_24h)
    paired_7d = pair_trades(trades_7d)
    paired_30d = pair_trades(trades_30d)

    n_24h = len(paired_24h)
    n_7d = len(paired_7d)
    n_30d = len(paired_30d)
    n_signals = len(signals_24h)

    # --- ゲート1: 最低データ量 ---
    MIN_TRADES_24H = 3   # 日次3件未満は統計的に無意味
    MIN_TRADES_7D = 5    # 週次5件未満は傾向判断不可
    has_enough_data = (n_7d >= MIN_TRADES_7D)

    # --- ゲート2: 直近改善のクールダウン ---
    # PARAM_TUNE/CODE_CHANGE が直近7日以内にあったら抑制
    COOLDOWN_DAYS = 7
    active_changes = [
        imp for imp in recent_improvements
        if str(imp.get('decision', '')) in ('PARAM_TUNE', 'CODE_CHANGE')
    ]
    days_since_last_change = None
    if active_changes:
        latest = max(active_changes, key=lambda x: int(float(x.get('timestamp', 0))))
        days_since_last_change = round(
            (int(time.time()) - int(float(latest.get('timestamp', 0)))) / 86400, 1
        )
    cooldown_ok = (days_since_last_change is None or days_since_last_change >= COOLDOWN_DAYS)

    # --- ゲート3: 勝率の信頼区間 (Wilson score interval) ---
    # n が小さいときは信頼区間が広すぎるので変更すべきでない
    def wilson_interval(wins, total, z=1.96):
        """95% Wilson score interval for binomial proportion"""
        if total == 0:
            return (0, 1.0, 1.0)  # (lower, upper, width)
        p = wins / total
        denom = 1 + z**2 / total
        centre = (p + z**2 / (2 * total)) / denom
        margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
        lower = max(0, centre - margin)
        upper = min(1, centre + margin)
        return (round(lower, 4), round(upper, 4), round(upper - lower, 4))

    wins_7d = len([t for t in paired_7d if t['pnl'] > 0])
    ci_lower, ci_upper, ci_width = wilson_interval(wins_7d, n_7d)

    # 信頼区間が0.40以上なら「どちらとも言えない」→ 変更抑制
    CI_WIDTH_THRESHOLD = 0.40
    statistically_significant = (ci_width < CI_WIDTH_THRESHOLD)

    # --- ゲート4: 全通貨同方向チェック (市場全体の動き) ---
    pair_pnls = {}
    for t in paired_24h:
        pair = t.get('pair', 'unknown')
        if pair not in pair_pnls:
            pair_pnls[pair] = []
        pair_pnls[pair].append(t['pnl'])
    if pair_pnls:
        pair_directions = [sum(pnls) > 0 for pnls in pair_pnls.values()]
        all_same_direction = all(pair_directions) or not any(pair_directions)
    else:
        all_same_direction = False

    # --- 総合判定 ---
    # allow_improvement: 全ゲート通過時のみ True
    skip_reasons = []
    if not has_enough_data:
        skip_reasons.append(f'insufficient_data(7d={n_7d}<{MIN_TRADES_7D})')
    if not cooldown_ok:
        skip_reasons.append(f'cooldown(last_change={days_since_last_change}d<{COOLDOWN_DAYS}d)')
    if not statistically_significant:
        skip_reasons.append(f'wide_ci(width={ci_width:.3f}>={CI_WIDTH_THRESHOLD})')
    if all_same_direction and n_24h >= 2:
        skip_reasons.append(f'market_wide_move({len(pair_pnls)}pairs_same_dir)')

    allow_improvement = len(skip_reasons) == 0

    # 信頼度スコア (0.0-1.0)
    confidence_score = 0.0
    if n_7d > 0:
        data_factor = min(1.0, n_7d / 20)           # 20件で満点
        ci_factor = max(0, 1.0 - ci_width / 0.5)    # CI幅が狭いほど高い
        cooldown_factor = 1.0 if cooldown_ok else 0.3
        market_factor = 0.5 if all_same_direction else 1.0
        confidence_score = round(
            data_factor * 0.35 + ci_factor * 0.35 + cooldown_factor * 0.15 + market_factor * 0.15,
            3
        )

    return {
        'allow_improvement': allow_improvement,
        'confidence_score': confidence_score,
        'skip_reasons': skip_reasons,
        'trades_24h_paired': n_24h,
        'trades_7d_paired': n_7d,
        'trades_30d_paired': n_30d,
        'signals_24h': n_signals,
        'win_rate_7d': round(wins_7d / n_7d, 4) if n_7d > 0 else 0,
        'win_rate_7d_ci': {'lower': ci_lower, 'upper': ci_upper, 'width': ci_width},
        'days_since_last_change': days_since_last_change,
        'all_same_direction': all_same_direction,
        'thresholds': {
            'min_trades_7d': MIN_TRADES_7D,
            'cooldown_days': COOLDOWN_DAYS,
            'ci_width_threshold': CI_WIDTH_THRESHOLD
        }
    }


def build_report(date, timestamp, trades_24h, trades_7d, trades_30d,
                 signals_24h, active_positions, market_context,
                 recent_improvements) -> dict:
    """構造化レポートを生成"""
    data_quality = build_data_quality(
        trades_24h, trades_7d, trades_30d, signals_24h, recent_improvements
    )
    return {
        'date': date,
        'timestamp': timestamp,
        'data_quality': data_quality,
        'trades': build_trade_stats(trades_24h, trades_7d, trades_30d),
        'signals': build_signal_stats(signals_24h),
        'positions': build_position_stats(active_positions, timestamp),
        'market_summary': build_market_summary(market_context),
        'rolling_7d': build_rolling_stats(trades_7d),
        'rolling_30d': build_rolling_stats(trades_30d),
        'recent_improvements': [
            {
                'id': str(imp.get('improvement_id', '')),
                'date': str(imp.get('date', '')),
                'decision': str(imp.get('decision', '')),
                'summary': str(imp.get('summary', ''))[:200],
                'changes_applied': imp.get('changes_applied', [])
            }
            for imp in recent_improvements[:5]
        ]
    }


def build_trade_stats(trades_24h, trades_7d, trades_30d) -> dict:
    """取引統計を計算"""
    # BUYとSELLをペアリング (簡易: 同一通貨のBUY→SELL連続をマッチ)
    paired = pair_trades(trades_24h)

    wins = [t for t in paired if t['pnl'] > 0]
    losses = [t for t in paired if t['pnl'] <= 0]

    total_pnl = sum(t['pnl'] for t in paired)
    win_rate = len(wins) / len(paired) if paired else 0

    details = []
    for t in paired:
        detail = {
            'pair': t['pair'],
            'pnl': round(t['pnl'], 2),
            'entry_score': t.get('entry_score', 0),
            'hold_minutes': t.get('hold_minutes', 0),
            'components_at_entry': t.get('components', {}),
            'buy_threshold': t.get('buy_threshold', 0),
        }
        details.append(detail)

    return {
        'total': len(paired),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(win_rate, 4),
        'total_pnl_jpy': round(total_pnl, 2),
        'avg_hold_minutes': round(
            sum(t.get('hold_minutes', 0) for t in paired) / len(paired), 1
        ) if paired else 0,
        'details': details,
        'raw_buy_count': len([t for t in trades_24h if str(t.get('action', '')) == 'buy']),
        'raw_sell_count': len([t for t in trades_24h if str(t.get('action', '')) == 'sell']),
    }


def pair_trades(trades: list) -> list:
    """BUY→SELLをペアリングしてPnLを計算"""
    paired = []
    open_buys = defaultdict(list)  # pair -> [buy_trade, ...]

    for t in trades:
        pair = str(t.get('pair', ''))
        action = str(t.get('action', '')).lower()
        rate = float(t.get('rate', 0))
        amount = float(t.get('amount', 0))
        ts = int(float(t.get('timestamp', 0)))

        if action == 'buy':
            open_buys[pair].append({
                'pair': pair,
                'rate': rate,
                'amount': amount,
                'timestamp': ts,
                'entry_score': float(t.get('technical_score', 0)) * float(t.get('weight_technical', 0.45))
                              + float(t.get('chronos_score', 0)) * float(t.get('weight_chronos', 0.25))
                              + float(t.get('sentiment_score', 0)) * float(t.get('weight_sentiment', 0.15)),
                'components': {
                    'technical': float(t.get('technical_score', 0)),
                    'chronos': float(t.get('chronos_score', 0)),
                    'sentiment': float(t.get('sentiment_score', 0)),
                },
                'buy_threshold': float(t.get('buy_threshold', 0)),
            })
        elif action == 'sell' and open_buys[pair]:
            buy = open_buys[pair].pop(0)
            pnl = (rate - buy['rate']) * buy['amount']
            hold_minutes = (ts - buy['timestamp']) / 60
            paired.append({
                'pair': pair,
                'pnl': pnl,
                'entry_score': buy.get('entry_score', 0),
                'hold_minutes': round(hold_minutes, 1),
                'components': buy.get('components', {}),
                'buy_threshold': buy.get('buy_threshold', 0),
                'buy_rate': buy['rate'],
                'sell_rate': rate,
            })

    return paired


def build_signal_stats(signals: list) -> dict:
    """シグナル統計"""
    if not signals:
        return {'total': 0, 'buy_count': 0, 'sell_count': 0, 'hold_count': 0,
                'avg_score': 0, 'component_stats': {}, 'score_distribution': {}}

    total = len(signals)
    buy_count = 0
    sell_count = 0
    hold_count = 0
    scores = []

    tech_scores = []
    chronos_scores = []
    sent_scores = []
    mkt_scores = []

    for s in signals:
        signal = str(s.get('signal', 'HOLD')).upper()
        if signal == 'BUY':
            buy_count += 1
        elif signal == 'SELL':
            sell_count += 1
        else:
            hold_count += 1

        score = float(s.get('total_score', s.get('score', 0)))
        scores.append(score)

        ts = float(s.get('technical_score', 0))
        cs = float(s.get('chronos_score', 0))
        ss = float(s.get('sentiment_score', 0))
        ms = float(s.get('market_context_score', 0))
        tech_scores.append(ts)
        chronos_scores.append(cs)
        sent_scores.append(ss)
        mkt_scores.append(ms)

    # スコア分布
    dist = {'<-0.3': 0, '-0.3~-0.1': 0, '-0.1~0.1': 0, '0.1~0.3': 0, '>0.3': 0}
    for sc in scores:
        if sc < -0.3:
            dist['<-0.3'] += 1
        elif sc < -0.1:
            dist['-0.3~-0.1'] += 1
        elif sc < 0.1:
            dist['-0.1~0.1'] += 1
        elif sc < 0.3:
            dist['0.1~0.3'] += 1
        else:
            dist['>0.3'] += 1

    def stats(vals):
        if not vals:
            return {'mean': 0, 'std': 0}
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = variance ** 0.5
        return {'mean': round(mean, 4), 'std': round(std, 4)}

    chronos_stats = stats(chronos_scores)
    near_zero = len([c for c in chronos_scores if abs(c) < 0.05])
    chronos_stats['near_zero_rate'] = round(near_zero / len(chronos_scores), 4) if chronos_scores else 0

    return {
        'total': total,
        'buy_count': buy_count,
        'sell_count': sell_count,
        'hold_count': hold_count,
        'avg_score': round(sum(scores) / len(scores), 4) if scores else 0,
        'score_distribution': dist,
        'component_stats': {
            'technical': stats(tech_scores),
            'chronos': chronos_stats,
            'sentiment': stats(sent_scores),
            'market_context': stats(mkt_scores),
        }
    }


def build_position_stats(positions: list, now: int) -> list:
    """アクティブポジション情報"""
    result = []
    for p in positions:
        entry_price = float(p.get('entry_price', 0))
        entry_time = int(float(p.get('entry_time', 0)))
        hold_minutes = (now - entry_time) / 60 if entry_time > 0 else 0
        result.append({
            'pair': str(p.get('pair', '')),
            'entry_price': entry_price,
            'amount': float(p.get('amount', 0)),
            'hold_minutes': round(hold_minutes, 1),
            'stop_loss': float(p.get('stop_loss', 0)),
            'take_profit': float(p.get('take_profit', 0)),
        })
    return result


def build_market_summary(ctx: dict) -> dict:
    """市場環境サマリ"""
    if not ctx:
        return {'available': False}

    components = ctx.get('components', {})
    fg = components.get('fear_greed', {})
    funding = components.get('funding_rate', {})
    dom = components.get('btc_dominance', {})

    return {
        'available': True,
        'market_score': float(ctx.get('market_score', 0)),
        'fear_greed_value': int(fg.get('value', 50)),
        'fear_greed_class': str(fg.get('classification', 'N/A')),
        'funding_avg': float(funding.get('avg_rate', 0)),
        'btc_dominance': float(dom.get('value', 50)),
        'timestamp': int(float(ctx.get('timestamp', 0))),
    }


def build_rolling_stats(trades: list) -> dict:
    """ローリング統計 (7d/30d)"""
    paired = pair_trades(trades)
    if not paired:
        return {'win_rate': 0, 'total_pnl': 0, 'trades': 0}

    wins = len([t for t in paired if t['pnl'] > 0])
    total_pnl = sum(t['pnl'] for t in paired)

    return {
        'win_rate': round(wins / len(paired), 4) if paired else 0,
        'total_pnl': round(total_pnl, 2),
        'trades': len(paired),
    }


# =============================================================================
# 出力
# =============================================================================

def save_to_s3(report: dict, key: str):
    """S3にレポートJSON保存"""
    try:
        s3.put_object(
            Bucket=REPORT_BUCKET,
            Key=key,
            Body=json.dumps(report, cls=DecimalEncoder, ensure_ascii=False, indent=2),
            ContentType='application/json'
        )
    except Exception as e:
        print(f"S3 save error: {e}")


def send_slack_summary(report: dict):
    """Slackに日次サマリー送信"""
    if not SLACK_WEBHOOK_URL:
        return

    trades = report['trades']
    signals = report['signals']
    market = report['market_summary']
    r7d = report['rolling_7d']
    r30d = report['rolling_30d']
    positions = report['positions']

    # 勝率の色付きアイコン
    wr_icon = '🟢' if trades['win_rate'] >= 0.5 else '🔴' if trades['win_rate'] < 0.3 else '🟡'
    pnl_icon = '📈' if trades['total_pnl_jpy'] >= 0 else '📉'

    # 市場環境
    mkt_line = ""
    if market.get('available'):
        mkt_line = (
            f"🌍 *市場環境:* F&G={market['fear_greed_value']} ({market['fear_greed_class']}) "
            f"| Funding={market['funding_avg']:.4%} | BTC Dom={market['btc_dominance']:.1f}%"
        )

    # ポジション
    pos_line = f"📂 *保有ポジション:* {len(positions)}件"
    for p in positions:
        pos_line += f"\n   {p['pair']}: ¥{p['entry_price']:,.0f} ({p['hold_minutes']:.0f}分保有)"

    # トレード詳細
    detail_lines = ""
    for d in trades['details'][:5]:
        icon = '✅' if d['pnl'] > 0 else '❌'
        comp = d.get('components_at_entry', {})
        comp_str = ' | '.join([f"T:{comp.get('technical',0):.2f}",
                               f"AI:{comp.get('chronos',0):.2f}",
                               f"S:{comp.get('sentiment',0):.2f}"])
        detail_lines += f"\n   {icon} {d['pair']}: ¥{d['pnl']:+,.0f} ({d['hold_minutes']:.0f}分) [{comp_str}]"

    # 直近の改善
    improve_line = ""
    if report.get('recent_improvements'):
        latest = report['recent_improvements'][0]
        improve_line = f"\n🔧 *直近の改善:* {latest.get('summary', 'N/A')[:100]}"

    text = f"""📊 *日次レポート: {report['date']}*

{pnl_icon} *本日の成績:*
   取引: {trades['total']}件 | 勝率: {wr_icon} {trades['win_rate']:.0%} ({trades['wins']}勝{trades['losses']}敗)
   損益: ¥{trades['total_pnl_jpy']:+,.0f} | 平均保有: {trades['avg_hold_minutes']:.0f}分{detail_lines}

📡 *シグナル統計:* {signals['total']}件 (BUY:{signals['buy_count']} SELL:{signals['sell_count']} HOLD:{signals['hold_count']})
   平均スコア: {signals['avg_score']:+.4f}

{mkt_line}
{pos_line}

📈 *ローリング:* 7d: {r7d['trades']}件 勝率{r7d['win_rate']:.0%} ¥{r7d['total_pnl']:+,.0f} | 30d: {r30d['trades']}件 勝率{r30d['win_rate']:.0%} ¥{r30d['total_pnl']:+,.0f}{improve_line}

🤖 _自動改善分析を実行中..._"""

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text}
            }
        ]
    }

    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=10)
        print(f"Slack summary sent (status: {response.status})")
    except Exception as e:
        print(f"Slack send failed: {e}")


def trigger_auto_improve(report: dict):
    """GitHub Actions auto-improve ワークフローをトリガー"""
    token = get_github_token()
    if not token:
        print("No GitHub token, skipping auto-improve trigger")
        return
    if not GITHUB_REPO:
        print("GITHUB_REPO not set")
        return

    # レポートを圧縮（GitHub dispatch payload上限 65KB 対策）
    compact_report = {
        'date': report['date'],
        'timestamp': report['timestamp'],
        'data_quality': report.get('data_quality', {}),
        'trades': report['trades'],
        'signals': {
            'total': report['signals']['total'],
            'buy_count': report['signals']['buy_count'],
            'sell_count': report['signals']['sell_count'],
            'hold_count': report['signals']['hold_count'],
            'avg_score': report['signals']['avg_score'],
            'score_distribution': report['signals']['score_distribution'],
            'component_stats': report['signals']['component_stats'],
        },
        'positions': report['positions'],
        'market_summary': report['market_summary'],
        'rolling_7d': report['rolling_7d'],
        'rolling_30d': report['rolling_30d'],
        'recent_improvements': report.get('recent_improvements', []),
    }

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    payload = {
        "event_type": "daily-improvement",
        "client_payload": {
            "report": json.loads(json.dumps(compact_report, cls=DecimalEncoder))
        }
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json',
                'User-Agent': 'eth-trading-daily-reporter'
            },
            method='POST'
        )
        response = urllib.request.urlopen(req, timeout=10)
        print(f"GitHub Actions auto-improve triggered (status: {response.status})")
    except Exception as e:
        print(f"GitHub Actions trigger failed: {e}")


def get_github_token() -> str:
    if not GITHUB_TOKEN_SECRET_ARN:
        return ''
    try:
        response = secrets.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        secret = json.loads(response['SecretString'])
        return secret.get('token', '')
    except Exception as e:
        print(f"Failed to get GitHub token: {e}")
        return ''


def notify_error(error_msg: str):
    """エラー発生時のSlack通知"""
    if not SLACK_WEBHOOK_URL:
        return
    payload = {
        "blocks": [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"❌ *Daily Reporter エラー*\n```{error_msg[:1000]}```"
            }
        }]
    }
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    urllib.request.urlopen(req, timeout=5)
