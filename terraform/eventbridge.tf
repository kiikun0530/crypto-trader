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

# 分析ワークフロー (5分間隔、EventBridge → Step Functions 直接起動)
resource "aws_cloudwatch_event_rule" "analysis_workflow" {
  name                = "${local.name_prefix}-analysis-workflow"
  description         = "Start analysis workflow every 5 minutes"
  schedule_expression = "rate(5 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-analysis-workflow"
  }
}

resource "aws_cloudwatch_event_target" "analysis_workflow" {
  rule      = aws_cloudwatch_event_rule.analysis_workflow.name
  target_id = "AnalysisWorkflow"
  arn       = aws_sfn_state_machine.analysis_workflow.arn
  role_arn  = aws_iam_role.eventbridge_execution.arn

  input = jsonencode({
    pairs     = keys(jsondecode(trimspace(var.trading_pairs_config)))
    timestamp = 0
  })
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

# マーケットコンテキスト収集ルール (30分間隔 - F&G, Funding Rate, BTC Dominance)
resource "aws_cloudwatch_event_rule" "market_context" {
  name                = "${local.name_prefix}-market-context"
  description         = "Collect market context (Fear&Greed, Funding, BTC Dominance) every 30 minutes"
  schedule_expression = "rate(30 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-market-context"
  }
}

resource "aws_cloudwatch_event_target" "market_context" {
  rule      = aws_cloudwatch_event_rule.market_context.name
  target_id = "MarketContextLambda"
  arn       = aws_lambda_function.functions["market-context"].arn
}

resource "aws_lambda_permission" "market_context" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["market-context"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.market_context.arn
}


