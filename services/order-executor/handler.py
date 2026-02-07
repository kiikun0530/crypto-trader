"""
注文実行 Lambda
SQSからシグナルを受信し、Coincheck APIで注文実行

改善点:
- 日本円残高確認
- スコアに応じた投資金額調整（期待値連動）
- 手数料考慮（ETH取引所: 0%、将来変更に備えてパラメータ化）
- 最小注文量チェック
"""
import json
import os
import time
import hmac
import hashlib
import urllib.request
import boto3
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
secrets = boto3.client('secretsmanager')

POSITIONS_TABLE = os.environ.get('POSITIONS_TABLE', 'eth-trading-positions')
TRADES_TABLE = os.environ.get('TRADES_TABLE', 'eth-trading-trades')
NOTIFICATIONS_TOPIC_ARN = os.environ.get('NOTIFICATIONS_TOPIC_ARN', '')
COINCHECK_SECRET_ARN = os.environ.get('COINCHECK_SECRET_ARN', '')
MAX_POSITION_JPY = float(os.environ.get('MAX_POSITION_JPY', '15000'))

# 手数料設定（Coincheck取引所 ETH: 0%、将来変更に備えてパラメータ化）
MAKER_FEE_RATE = float(os.environ.get('MAKER_FEE_RATE', '0.0'))  # 0%
TAKER_FEE_RATE = float(os.environ.get('TAKER_FEE_RATE', '0.0'))  # 0%（成行はTaker）

# 最小注文金額（Coincheck: 500円相当）
MIN_ORDER_JPY = float(os.environ.get('MIN_ORDER_JPY', '500'))

# 予備資金（常に残しておく金額）
RESERVE_JPY = float(os.environ.get('RESERVE_JPY', '1000'))

# スコア閾値と投資比率（期待値連動）
SCORE_THRESHOLDS = [
    (0.90, 1.00),   # スコア0.90以上 → 利用可能残高の100%
    (0.80, 0.75),   # スコア0.80-0.90 → 75%
    (0.70, 0.50),   # スコア0.70-0.80 → 50%
    (0.65, 0.30),   # スコア0.65-0.70 → 30%
]

def handler(event, context):
    """注文実行"""
    for record in event.get('Records', []):
        try:
            body = json.loads(record['body'])
            process_order(body)
        except Exception as e:
            print(f"Error processing order: {str(e)}")
            raise  # DLQへ送信
    
    return {'statusCode': 200, 'body': 'OK'}

def process_order(order: dict):
    """注文処理"""
    pair = order['pair']
    signal = order['signal']
    score = order['score']
    
    # 1. 現在のポジション確認
    current_position = get_position(pair)
    
    # 2. 注文判定
    if signal == 'BUY':
        if current_position and current_position.get('side') == 'long':
            print(f"Already have long position for {pair}")
            return
        
        # 買い注文
        execute_buy(pair, score)
        
    elif signal == 'SELL':
        if not current_position or current_position.get('side') != 'long':
            print(f"No long position to sell for {pair}")
            return
        
        # 売り注文
        execute_sell(pair, current_position, score)

def get_position(pair: str) -> dict:
    """現在のポジション取得"""
    table = dynamodb.Table(POSITIONS_TABLE)
    response = table.query(
        KeyConditionExpression='pair = :pair',
        ExpressionAttributeValues={':pair': pair},
        ScanIndexForward=False,
        Limit=1
    )
    items = response.get('Items', [])
    if items and not items[0].get('closed'):
        return items[0]
    return None

def get_balance() -> dict:
    """Coincheck APIで残高取得"""
    try:
        creds = get_api_credentials()
        if not creds:
            print("No API credentials for balance check")
            return {'jpy': 0, 'eth': 0}
        
        result = call_coincheck_api('/api/accounts/balance', 'GET', None, creds)
        
        if result and result.get('success'):
            return {
                'jpy': float(result.get('jpy', 0)),
                'jpy_reserved': float(result.get('jpy_reserved', 0)),
                'eth': float(result.get('eth', 0)),
                'eth_reserved': float(result.get('eth_reserved', 0))
            }
        else:
            print(f"Balance API error: {result}")
            return {'jpy': 0, 'eth': 0}
            
    except Exception as e:
        print(f"Error getting balance: {str(e)}")
        return {'jpy': 0, 'eth': 0}

def calculate_order_amount(score: float, available_jpy: float) -> float:
    """
    スコアに応じた投資金額を計算（期待値連動）
    
    - 高スコア（高期待値）→ 多く投資
    - 低スコア（低期待値）→ 少なく投資
    - 最大金額はMAX_POSITION_JPYで制限
    - 手数料を考慮
    """
    # スコアに応じた投資比率を決定
    ratio = 0.0
    for threshold, r in SCORE_THRESHOLDS:
        if score >= threshold:
            ratio = r
            break
    
    if ratio == 0:
        print(f"Score {score} below minimum threshold, skipping order")
        return 0
    
    # 投資金額計算
    order_amount = available_jpy * ratio
    
    # 手数料を考慮（成行注文はTaker手数料）
    if TAKER_FEE_RATE > 0:
        order_amount = order_amount / (1 + TAKER_FEE_RATE)
    
    # 上限・下限チェック
    order_amount = min(order_amount, MAX_POSITION_JPY)
    
    if order_amount < MIN_ORDER_JPY:
        print(f"Order amount ¥{order_amount:,.0f} below minimum ¥{MIN_ORDER_JPY:,.0f}")
        return 0
    
    return order_amount

def execute_buy(pair: str, score: float):
    """買い注文実行（残高確認・スコア連動金額）"""
    timestamp = int(time.time())
    
    # 1. 残高確認
    balance = get_balance()
    available_jpy = balance.get('jpy', 0) - balance.get('jpy_reserved', 0) - RESERVE_JPY
    
    print(f"Balance: ¥{balance.get('jpy', 0):,.0f} (reserved: ¥{balance.get('jpy_reserved', 0):,.0f})")
    print(f"Available for trading: ¥{available_jpy:,.0f} (after reserve: ¥{RESERVE_JPY:,.0f})")
    
    if available_jpy <= 0:
        print("Insufficient JPY balance")
        send_notification(f"⚠️ 残高不足\n利用可能残高: ¥{available_jpy:,.0f}")
        return
    
    # 2. スコアに応じた投資金額計算
    order_amount = calculate_order_amount(score, available_jpy)
    
    if order_amount <= 0:
        print(f"Order amount is 0 (score: {score}, available: ¥{available_jpy:,.0f})")
        return
    
    print(f"Order amount: ¥{order_amount:,.0f} (score: {score:.3f}, ratio: {order_amount/available_jpy*100:.1f}%)")
    
    # 3. Coincheck APIで成行買い
    result = place_market_order(pair, 'buy', order_amount)
    
    if result and result.get('success'):
        # ポジション保存
        save_position(pair, timestamp, 'long', result, order_amount)
        
        # 取引履歴保存
        save_trade(pair, timestamp, 'BUY', result)
        
        # 通知（投資比率も表示）
        ratio_pct = (order_amount / available_jpy) * 100
        send_notification(
            f"🟢 ETH買い約定\n"
            f"金額: ¥{order_amount:,.0f} ({ratio_pct:.0f}%)\n"
            f"スコア: {score:.3f}\n"
            f"残高: ¥{available_jpy - order_amount:,.0f}"
        )
    else:
        error_msg = result.get('error', 'Unknown error') if result else 'API call failed'
        print(f"Buy order failed: {error_msg}")
        send_notification(f"❌ 買い注文失敗\nエラー: {error_msg}")

def execute_sell(pair: str, position: dict, score: float):
    """売り注文実行"""
    timestamp = int(time.time())
    
    amount = float(position.get('amount', 0))
    if amount <= 0:
        print("No ETH amount in position")
        return
    
    # 残高確認（念のため）
    balance = get_balance()
    available_eth = balance.get('eth', 0) - balance.get('eth_reserved', 0)
    
    if available_eth < amount:
        print(f"ETH balance mismatch: position={amount}, available={available_eth}")
        # 実際にある分だけ売る
        amount = available_eth
        if amount <= 0:
            send_notification(f"⚠️ ETH残高不足\n保有: {available_eth:.6f} ETH")
            return
    
    # Coincheck APIで成行売り
    result = place_market_order(pair, 'sell', amount_eth=amount)
    
    if result and result.get('success'):
        # ポジションクローズ
        close_position(pair, position, timestamp, result)
        
        # 取引履歴保存
        save_trade(pair, timestamp, 'SELL', result)
        
        # P/L計算（手数料考慮）
        entry_price = float(position.get('entry_price', 0))
        exit_price = float(result.get('rate', 0))
        gross_pnl = (exit_price - entry_price) * amount
        
        # 手数料計算（売りのTaker手数料）
        sell_fee = exit_price * amount * TAKER_FEE_RATE
        net_pnl = gross_pnl - sell_fee
        
        # 通知
        emoji = '💰' if net_pnl > 0 else '💸'
        fee_info = f"\n手数料: ¥{sell_fee:,.0f}" if sell_fee > 0 else ""
        send_notification(
            f"{emoji} ETH売り約定\n"
            f"数量: {amount:.6f} ETH\n"
            f"P/L: ¥{net_pnl:,.0f}{fee_info}\n"
            f"スコア: {score:.3f}"
        )
    else:
        error_msg = result.get('error', 'Unknown error') if result else 'API call failed'
        print(f"Sell order failed: {error_msg}")
        send_notification(f"❌ 売り注文失敗\nエラー: {error_msg}")

def place_market_order(pair: str, side: str, amount_jpy: float = None, amount_eth: float = None) -> dict:
    """成行注文（Coincheck API）"""
    try:
        # APIキー取得
        creds = get_api_credentials()
        if not creds:
            print("No API credentials found")
            return {'success': False, 'error': 'no_credentials'}
        
        # 注文パラメータ
        params = {
            'pair': pair,
            'order_type': f'market_{side}'
        }
        
        if side == 'buy' and amount_jpy:
            params['market_buy_amount'] = str(amount_jpy)
        elif side == 'sell' and amount_eth:
            params['amount'] = str(amount_eth)
        
        print(f"Placing order: {params}")
        
        # API呼び出し
        result = call_coincheck_api('/api/exchange/orders', 'POST', params, creds)
        print(f"Order result: {result}")
        return result
        
    except Exception as e:
        print(f"Order error: {str(e)}")
        return {'success': False, 'error': str(e)}

def get_api_credentials() -> dict:
    """Secrets Managerからクレデンシャル取得"""
    if not COINCHECK_SECRET_ARN:
        return None
    
    try:
        response = secrets.get_secret_value(SecretId=COINCHECK_SECRET_ARN)
        return json.loads(response['SecretString'])
    except:
        return None

def call_coincheck_api(path: str, method: str, params: dict, creds: dict) -> dict:
    """Coincheck API呼び出し"""
    base_url = 'https://coincheck.com'
    nonce = str(int(time.time() * 1000000))
    
    if method == 'GET':
        body = ''
    else:
        body = json.dumps(params) if params else ''
    
    message = nonce + base_url + path + body
    
    signature = hmac.new(
        creds['secret_key'].encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        'ACCESS-KEY': creds['access_key'],
        'ACCESS-NONCE': nonce,
        'ACCESS-SIGNATURE': signature,
        'Content-Type': 'application/json'
    }
    
    req = urllib.request.Request(
        base_url + path,
        data=body.encode() if body else None,
        headers=headers,
        method=method
    )
    
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())

def save_position(pair: str, timestamp: int, side: str, result: dict, order_amount_jpy: float = None):
    """ポジション保存"""
    table = dynamodb.Table(POSITIONS_TABLE)
    
    amount = result.get('amount', 0)
    rate = result.get('rate', 0)
    
    table.put_item(Item={
        'pair': pair,
        'position_id': f"{timestamp}",
        'side': side,
        'amount': Decimal(str(amount)),
        'entry_price': Decimal(str(rate)),
        'entry_time': timestamp,
        'order_amount_jpy': Decimal(str(order_amount_jpy or 0)),
        'stop_loss': Decimal(str(float(rate) * 0.95)),
        'take_profit': Decimal(str(float(rate) * 1.10)),
        'closed': False
    })

def close_position(pair: str, position: dict, timestamp: int, result: dict):
    """ポジションクローズ"""
    table = dynamodb.Table(POSITIONS_TABLE)
    table.update_item(
        Key={'pair': pair, 'position_id': position['position_id']},
        UpdateExpression='SET closed = :closed, exit_price = :exit, exit_time = :time',
        ExpressionAttributeValues={
            ':closed': True,
            ':exit': Decimal(str(result.get('rate', 0))),
            ':time': timestamp
        }
    )

def save_trade(pair: str, timestamp: int, action: str, result: dict):
    """取引履歴保存"""
    table = dynamodb.Table(TRADES_TABLE)
    table.put_item(Item={
        'pair': pair,
        'timestamp': timestamp,
        'action': action,
        'amount': Decimal(str(result.get('amount', 0))),
        'rate': Decimal(str(result.get('rate', 0))),
        'order_id': result.get('id', ''),
        'fee_rate': Decimal(str(TAKER_FEE_RATE))
    })

def send_notification(message: str):
    """SNS通知送信"""
    if NOTIFICATIONS_TOPIC_ARN:
        sns.publish(
            TopicArn=NOTIFICATIONS_TOPIC_ARN,
            Subject='ETH Trading Alert',
            Message=message
        )
