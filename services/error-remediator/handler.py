"""
Error Remediator Lambda
CloudWatch Logs Subscription Filter からエラーログを受信し、
① Slack通知（即時アラート）
② GitHub Actions 自動修復ワークフローをトリガー

デバウンス機能:
- 同一Lambda関数のエラーは COOLDOWN_MINUTES 間隔で1回のみトリガー
- 連続エラーによるCI爆発を防止
"""
import json
import os
import base64
import gzip
import urllib.request
import time
import hashlib
import boto3

SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
GITHUB_TOKEN_SECRET_ARN = os.environ.get('GITHUB_TOKEN_SECRET_ARN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')
COOLDOWN_MINUTES = int(os.environ.get('COOLDOWN_MINUTES', '30'))

secrets = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')

# クールダウン管理用テーブル（DynamoDB）
COOLDOWN_TABLE = os.environ.get('ANALYSIS_STATE_TABLE', 'eth-trading-analysis-state')

# 無視するログパターン（正常動作内のエラー風ログ）
IGNORE_PATTERNS = [
    'REPORT RequestId',
    'INIT_START',
    'START RequestId',
    'END RequestId',
    'Task timed out',  # タイムアウトはMetric Alarmで検知
]


def handler(event, context):
    """CloudWatch Logs Subscription Filter イベント処理"""
    try:
        # CloudWatch Logs のデータをデコード
        log_data = decode_log_event(event)
        if not log_data:
            return {'statusCode': 200, 'body': 'No data'}

        log_group = log_data.get('logGroup', '')
        log_stream = log_data.get('logStream', '')
        log_events = log_data.get('logEvents', [])

        # Lambda関数名を抽出
        function_name = extract_function_name(log_group)
        if not function_name:
            print(f"Could not extract function name from: {log_group}")
            return {'statusCode': 200, 'body': 'Unknown function'}

        # エラーメッセージを収集
        error_messages = collect_error_messages(log_events)
        if not error_messages:
            print("No actionable error messages found")
            return {'statusCode': 200, 'body': 'No errors'}

        # クールダウンチェック（同一関数に対する連続トリガー防止）
        if is_in_cooldown(function_name):
            print(f"Cooldown active for {function_name}, skipping")
            return {'statusCode': 200, 'body': 'Cooldown'}

        # クールダウン設定
        set_cooldown(function_name)

        error_summary = '\n'.join(error_messages[:10])  # 最大10行
        print(f"Error detected in {function_name}: {error_summary[:500]}")

        # ① Slack通知
        send_slack_alert(function_name, error_summary, log_stream)

        # ② GitHub Actions トリガー
        trigger_auto_fix(function_name, error_summary, log_group, log_stream)

        return {'statusCode': 200, 'body': 'Processed'}

    except Exception as e:
        print(f"Error in error-remediator: {str(e)}")
        # 自身のエラーでは再帰しないよう、例外は握りつぶす
        return {'statusCode': 500, 'body': str(e)}


def decode_log_event(event: dict) -> dict:
    """CloudWatch Logs Subscription Filter のイベントをデコード"""
    try:
        compressed = base64.b64decode(event['awslogs']['data'])
        decompressed = gzip.decompress(compressed)
        return json.loads(decompressed)
    except Exception as e:
        print(f"Failed to decode log event: {e}")
        return None


def extract_function_name(log_group: str) -> str:
    """ロググループ名からLambda関数名の短縮名を抽出
    例: /aws/lambda/eth-trading-order-executor → order-executor
    """
    prefix = '/aws/lambda/eth-trading-'
    if log_group.startswith(prefix):
        return log_group[len(prefix):]
    # フルネームにフォールバック
    if log_group.startswith('/aws/lambda/'):
        return log_group.split('/')[-1]
    return ''


def collect_error_messages(log_events: list) -> list:
    """ログイベントからアクショナブルなエラーメッセージを抽出"""
    errors = []
    for event in log_events:
        message = event.get('message', '').strip()
        # 無視パターンをスキップ
        if any(pat in message for pat in IGNORE_PATTERNS):
            continue
        # 空行やREPORTスキップ
        if not message or message.startswith('REPORT') or message.startswith('END'):
            continue
        errors.append(message)
    return errors


def is_in_cooldown(function_name: str) -> bool:
    """クールダウン中かチェック"""
    try:
        table = dynamodb.Table(COOLDOWN_TABLE)
        result = table.get_item(
            Key={'key': f'error-cooldown-{function_name}'}
        )
        item = result.get('Item')
        if not item:
            return False

        last_triggered = int(item.get('value', 0))
        now = int(time.time())
        return (now - last_triggered) < (COOLDOWN_MINUTES * 60)
    except Exception as e:
        print(f"Cooldown check failed: {e}")
        return False  # エラー時はクールダウンなしとして処理


def set_cooldown(function_name: str):
    """クールダウンを設定"""
    try:
        table = dynamodb.Table(COOLDOWN_TABLE)
        table.put_item(Item={
            'key': f'error-cooldown-{function_name}',
            'value': str(int(time.time())),
            'function': function_name,
            'type': 'error-cooldown'
        })
    except Exception as e:
        print(f"Failed to set cooldown: {e}")


def send_slack_alert(function_name: str, error_summary: str, log_stream: str):
    """Slack にエラーアラートを送信"""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set")
        return

    # エラーメッセージを整形（長すぎる場合は切り詰め）
    truncated = error_summary[:1500] if len(error_summary) > 1500 else error_summary

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🚨 Lambda Error: {function_name}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*関数:* `eth-trading-{function_name}`\n"
                        f"*時刻:* <!date^{int(time.time())}^{{date_short_pretty}} {{time}}|{time.strftime('%Y-%m-%d %H:%M:%S')}>\n"
                        f"*ログストリーム:* `{log_stream[:80]}`"
                    )
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{truncated}```"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "🤖 Auto-fix ワークフローをトリガーしています..."
                    }
                ]
            }
        ]
    }

    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        response = urllib.request.urlopen(req, timeout=5)
        print(f"Slack alert sent (status: {response.status})")
    except Exception as e:
        print(f"Slack alert failed: {e}")


def get_github_token() -> str:
    """Secrets Manager から GitHub PAT を取得"""
    if not GITHUB_TOKEN_SECRET_ARN:
        return ''
    try:
        response = secrets.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)
        secret = json.loads(response['SecretString'])
        return secret.get('token', '')
    except Exception as e:
        print(f"Failed to get GitHub token: {e}")
        return ''


def trigger_auto_fix(function_name: str, error_summary: str, log_group: str, log_stream: str):
    """GitHub Actions の repository_dispatch をトリガー"""
    token = get_github_token()
    if not token:
        print("No GitHub token available, skipping auto-fix trigger")
        return

    if not GITHUB_REPO:
        print("GITHUB_REPO not set")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"

    payload = {
        "event_type": "lambda-error",
        "client_payload": {
            "function_name": function_name,
            "error_summary": error_summary[:3000],  # GitHub API payload制限
            "log_group": log_group,
            "log_stream": log_stream,
            "timestamp": int(time.time()),
            "service_dir": f"services/{function_name}/handler.py"
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
                'User-Agent': 'eth-trading-error-remediator'
            },
            method='POST'
        )
        response = urllib.request.urlopen(req, timeout=10)
        print(f"GitHub Actions triggered (status: {response.status})")
    except Exception as e:
        print(f"GitHub Actions trigger failed: {e}")
        # Slackにフォールバック通知
        send_slack_fallback(function_name, str(e))


def send_slack_fallback(function_name: str, error: str):
    """GitHub Actions トリガー失敗時のSlack通知"""
    if not SLACK_WEBHOOK_URL:
        return

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Auto-fix トリガー失敗*\n"
                        f"関数: `{function_name}`\n"
                        f"エラー: {error}\n"
                        f"手動での確認が必要です。"
                    )
                }
            }
        ]
    }

    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
