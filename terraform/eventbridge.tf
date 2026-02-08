# =============================================================================
# EventBridge Rules
# =============================================================================

# 価格収集ルール (5分間隔)
resource "aws_cloudwatch_event_rule" "price_collection" {
  name                = "${local.name_prefix}-price-collection"
  description         = "Collect all crypto prices every 5 minutes"
  schedule_expression = "rate(5 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-price-collection"
  }
}

resource "aws_cloudwatch_event_target" "price_collection" {
  rule      = aws_cloudwatch_event_rule.price_collection.name
  target_id = "PriceCollectorLambda"
  arn       = aws_lambda_function.functions["price-collector"].arn
}

resource "aws_lambda_permission" "price_collection" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["price-collector"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.price_collection.arn
}

# ポジション監視ルール (5分間隔)
resource "aws_cloudwatch_event_rule" "position_monitor" {
  name                = "${local.name_prefix}-position-monitor"
  description         = "Monitor all open positions every 5 minutes"
  schedule_expression = "rate(5 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-position-monitor"
  }
}

resource "aws_cloudwatch_event_target" "position_monitor" {
  rule      = aws_cloudwatch_event_rule.position_monitor.name
  target_id = "PositionMonitorLambda"
  arn       = aws_lambda_function.functions["position-monitor"].arn
}

resource "aws_lambda_permission" "position_monitor" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["position-monitor"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.position_monitor.arn
}

# ニュース収集ルール (30分間隔 - CryptoPanic Growth Plan)
resource "aws_cloudwatch_event_rule" "news_collection" {
  name                = "${local.name_prefix}-news-collection"
  description         = "Collect and analyze news every 30 minutes (Growth Plan: 3000 req/mo)"
  schedule_expression = "rate(30 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-news-collection"
  }
}

resource "aws_cloudwatch_event_target" "news_collection" {
  rule      = aws_cloudwatch_event_rule.news_collection.name
  target_id = "NewsCollectorLambda"
  arn       = aws_lambda_function.functions["news-collector"].arn
}

resource "aws_lambda_permission" "news_collection" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["news-collector"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.news_collection.arn
}

# 分析トリガールール (価格収集Lambdaからのイベント)
resource "aws_cloudwatch_event_rule" "analysis_trigger" {
  name        = "${local.name_prefix}-analysis-trigger"
  description = "Trigger analysis workflow based on price volatility"
  state       = "ENABLED"

  event_pattern = jsonencode({
    source      = ["eth-trading.price-collector"]
    detail-type = ["analysis-required"]
  })

  tags = {
    Name = "${local.name_prefix}-analysis-trigger"
  }
}

resource "aws_cloudwatch_event_target" "analysis_trigger" {
  rule      = aws_cloudwatch_event_rule.analysis_trigger.name
  target_id = "AnalysisWorkflow"
  arn       = aws_sfn_state_machine.analysis_workflow.arn
  role_arn  = aws_iam_role.eventbridge_execution.arn
}
