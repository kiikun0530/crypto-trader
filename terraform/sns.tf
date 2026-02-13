# =============================================================================
# SNS Topics
# =============================================================================

# 取引通知トピック
resource "aws_sns_topic" "notifications" {
  name = "${local.name_prefix}-notifications"

  tags = {
    Name = "${local.name_prefix}-notifications"
  }
}

# アラート通知トピック
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"

  tags = {
    Name = "${local.name_prefix}-alerts"
  }
}

# -----------------------------------------------------------------------------
# SNS Topic Policies
# -----------------------------------------------------------------------------
resource "aws_sns_topic_policy" "notifications" {
  arn = aws_sns_topic.notifications.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowLambdaPublish"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.notifications.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${local.name_prefix}-*"
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudWatchAlarms"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Slack Webhook URL (変数で設定)
# -----------------------------------------------------------------------------
variable "slack_webhook_url" {
  description = "Slack Webhook URL for notifications"
  type        = string
  default     = ""
  sensitive   = true
}

# -----------------------------------------------------------------------------
# Slack通知用Lambda関数
# -----------------------------------------------------------------------------
data "archive_file" "slack_notifier" {
  type        = "zip"
  output_path = "${path.module}/.terraform/tmp/slack_notifier.zip"
  
  source {
    content = <<-EOF
import json
import urllib.request
import os

def handler(event, context):
    webhook_url = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook_url:
        print("SLACK_WEBHOOK_URL not set")
        return
    
    for record in event.get('Records', []):
        message = record.get('Sns', {}).get('Message', '')
        subject = record.get('Sns', {}).get('Subject', 'AWS Notification')
        
        # Parse JSON if possible
        try:
            msg_obj = json.loads(message)
            formatted_message = json.dumps(msg_obj, indent=2, ensure_ascii=False)
        except:
            formatted_message = message
        
        slack_message = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": subject}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{formatted_message}```"}
                }
            ]
        }
        
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(slack_message).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req)
    
    return {'statusCode': 200}
EOF
    filename = "handler.py"
  }
}

resource "aws_cloudwatch_log_group" "slack_notifier" {
  count             = var.slack_webhook_url != "" ? 1 : 0
  name              = "/aws/lambda/${local.name_prefix}-slack-notifier"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-slack-notifier"
  }
}

resource "aws_lambda_function" "slack_notifier" {
  count            = var.slack_webhook_url != "" ? 1 : 0
  function_name    = "${local.name_prefix}-slack-notifier"
  role             = aws_iam_role.lambda_execution.arn
  handler          = "handler.handler"
  runtime          = "python3.11"
  timeout          = 30
  filename         = data.archive_file.slack_notifier.output_path
  source_code_hash = data.archive_file.slack_notifier.output_base64sha256

  environment {
    variables = {
      SLACK_WEBHOOK_URL = var.slack_webhook_url
    }
  }

  tags = {
    Name = "${local.name_prefix}-slack-notifier"
  }

  depends_on = [aws_cloudwatch_log_group.slack_notifier]
}

# notifications → Slack
resource "aws_sns_topic_subscription" "notifications_slack" {
  count     = var.slack_webhook_url != "" ? 1 : 0
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_notifier[0].arn
}

resource "aws_lambda_permission" "notifications_slack" {
  count         = var.slack_webhook_url != "" ? 1 : 0
  statement_id  = "AllowSNSNotifications"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier[0].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.notifications.arn
}

# alerts → Slack
resource "aws_sns_topic_subscription" "alerts_slack" {
  count     = var.slack_webhook_url != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_notifier[0].arn
}

resource "aws_lambda_permission" "alerts_slack" {
  count         = var.slack_webhook_url != "" ? 1 : 0
  statement_id  = "AllowSNSAlerts"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier[0].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts.arn
}

# DLQ CloudWatch Alarm は SQS 削除に伴い不要 (Phase 6)
# order-executor のエラーは Lambda エラーアラームで検知
