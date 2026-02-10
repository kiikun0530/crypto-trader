# =============================================================================
# Monitoring & Alerting
# =============================================================================
# Lambda全関数のエラー監視 + 自動修復パイプライントリガー
# =============================================================================

# -----------------------------------------------------------------------------
# CloudWatch Metric Alarms - 全Lambda関数のエラー検知
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.lambda_functions

  alarm_name          = "${local.name_prefix}-${each.key}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "${each.key} Lambda でエラーが発生しました"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = "${local.name_prefix}-${each.key}"
  }

  tags = {
    Name = "${local.name_prefix}-${each.key}-errors"
  }
}

# -----------------------------------------------------------------------------
# CloudWatch Metric Alarms - Lambda実行時間（タイムアウト警告）
# Duration がタイムアウトの80%を超えたらアラート
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  for_each = local.lambda_functions

  alarm_name          = "${local.name_prefix}-${each.key}-duration"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Maximum"
  threshold           = each.value.timeout * 1000 * 0.8 # タイムアウトの80%（ミリ秒）
  alarm_description   = "${each.key} Lambda の実行時間がタイムアウトに近づいています"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = "${local.name_prefix}-${each.key}"
  }

  tags = {
    Name = "${local.name_prefix}-${each.key}-duration"
  }
}

# -----------------------------------------------------------------------------
# CloudWatch Logs Subscription Filter - エラーログを error-remediator へ転送
# warm-up は除外（手動実行のため）
# -----------------------------------------------------------------------------
locals {
  monitored_functions = {
    for k, v in local.lambda_functions : k => v
    if k != "warm-up"
  }
}

resource "aws_cloudwatch_log_subscription_filter" "error_filter" {
  for_each = local.monitored_functions

  name            = "${local.name_prefix}-${each.key}-error-filter"
  log_group_name  = "/aws/lambda/${local.name_prefix}-${each.key}"
  # [ERROR] プレフィックスを持つログ、またはTraceback、または真のExceptionエラーを検出
  # ただし [INFO] や "expected behavior" を含む想定内のリトライログは除外
  filter_pattern  = "?\"[ERROR]\" ?Traceback ?\"raise Exception\" -\"[INFO]\" -\"expected behavior\" -\"retrying in\""
  destination_arn = aws_lambda_function.error_remediator.arn

  depends_on = [
    aws_lambda_permission.error_remediator_cloudwatch,
    aws_cloudwatch_log_group.lambda
  ]
}

# -----------------------------------------------------------------------------
# Error Remediator Lambda
# エラーログ受信 → Slack通知 + GitHub Actions自動修復トリガー
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "error_remediator" {
  name              = "/aws/lambda/${local.name_prefix}-error-remediator"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-error-remediator"
  }
}

data "archive_file" "error_remediator" {
  type        = "zip"
  source_dir  = "${path.module}/../services/error-remediator"
  output_path = "${path.module}/.terraform/tmp/error-remediator.zip"
}

resource "aws_lambda_function" "error_remediator" {
  function_name    = "${local.name_prefix}-error-remediator"
  description      = "エラーログ検知 → Slack通知 + GitHub Auto-Fix トリガー"
  role             = aws_iam_role.lambda_execution.arn
  handler          = "handler.handler"
  runtime          = "python3.11"
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.error_remediator.output_path
  source_code_hash = data.archive_file.error_remediator.output_base64sha256

  environment {
    variables = {
      SLACK_WEBHOOK_URL        = var.slack_webhook_url
      GITHUB_TOKEN_SECRET_ARN  = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:github/auto-fix-token"
      GITHUB_REPO              = var.github_repo
      COOLDOWN_MINUTES         = "30"
    }
  }

  depends_on = [aws_cloudwatch_log_group.error_remediator]

  tags = {
    Name = "${local.name_prefix}-error-remediator"
  }
}

# CloudWatch Logs から error-remediator Lambda を呼び出す権限
resource "aws_lambda_permission" "error_remediator_cloudwatch" {
  statement_id  = "AllowCloudWatchLogs"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.error_remediator.function_name
  principal     = "logs.${var.aws_region}.amazonaws.com"
  source_arn    = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-*:*"
}
