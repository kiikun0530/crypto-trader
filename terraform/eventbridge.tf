# =============================================================================
# EventBridge Rules (マルチタイムフレーム対応)
# =============================================================================

# -----------------------------------------------------------------------------
# TF別分析ワークフロー (同一State Machineをパラメータ違いで起動)
# 各ルールが timeframe パラメータを変えて Step Functions を起動する
# -----------------------------------------------------------------------------

locals {
  # 分析対象通貨ペア一覧
  analysis_pairs = keys(jsondecode(trimspace(var.trading_pairs_config)))

  # タイムフレーム別スケジュール定義
  timeframe_schedules = {
    "15m" = {
      schedule = "rate(15 minutes)"
      description = "15-minute timeframe analysis"
    }
    "1h" = {
      schedule = "rate(1 hour)"
      description = "1-hour timeframe analysis"
    }
    "4h" = {
      schedule = "rate(4 hours)"
      description = "4-hour timeframe analysis"
    }
    "1d" = {
      schedule = "cron(5 0 * * ? *)"  # UTC 00:05 (JST 09:05) — 日足確定後
      description = "Daily timeframe analysis"
    }
  }
}

# TF別 EventBridge ルール
resource "aws_cloudwatch_event_rule" "tf_analysis" {
  for_each = local.timeframe_schedules

  name                = "${local.name_prefix}-analysis-${each.key}"
  description         = each.value.description
  schedule_expression = each.value.schedule
  state               = "ENABLED"

  tags = {
    Name      = "${local.name_prefix}-analysis-${each.key}"
    Timeframe = each.key
  }
}

# TF別 EventBridge → Step Functions ターゲット
resource "aws_cloudwatch_event_target" "tf_analysis" {
  for_each = local.timeframe_schedules

  rule      = aws_cloudwatch_event_rule.tf_analysis[each.key].name
  target_id = "AnalysisWorkflow-${each.key}"
  arn       = aws_sfn_state_machine.analysis_workflow.arn
  role_arn  = aws_iam_role.eventbridge_execution.arn

  input = jsonencode({
    timeframe = each.key
    pairs     = local.analysis_pairs
  })
}

# -----------------------------------------------------------------------------
# メタアグリゲーター (15分間隔で全TFスコアを統合判定)
# EventBridge → Lambda 直接起動 (Step Functions不要)
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "meta_aggregator" {
  name                = "${local.name_prefix}-meta-aggregator"
  description         = "Aggregate all timeframe scores every 15 minutes"
  schedule_expression = "rate(15 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-meta-aggregator"
  }
}

resource "aws_cloudwatch_event_target" "meta_aggregator" {
  rule      = aws_cloudwatch_event_rule.meta_aggregator.name
  target_id = "MetaAggregatorLambda"
  arn       = aws_lambda_function.functions["aggregator"].arn

  input = jsonencode({
    mode = "meta_aggregate"
  })
}

resource "aws_lambda_permission" "meta_aggregator" {
  statement_id  = "AllowEventBridgeMetaAggregate"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["aggregator"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.meta_aggregator.arn
}

# -----------------------------------------------------------------------------
# 注文実行 (15分間隔 — メタアグリゲーター後にDynamoDBシグナル読み取り)
# EventBridge → Lambda 直接起動
# meta-aggregatorがシグナル保存後に実行されるよう、同じ15分間隔で起動
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "order_executor" {
  name                = "${local.name_prefix}-order-executor"
  description         = "Execute orders based on latest signals every 15 minutes"
  schedule_expression = "rate(15 minutes)"
  state               = "ENABLED"

  tags = {
    Name = "${local.name_prefix}-order-executor"
  }
}

resource "aws_cloudwatch_event_target" "order_executor" {
  rule      = aws_cloudwatch_event_rule.order_executor.name
  target_id = "OrderExecutorLambda"
  arn       = aws_lambda_function.functions["order-executor"].arn
}

resource "aws_lambda_permission" "order_executor" {
  statement_id  = "AllowEventBridgeOrderExecutor"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["order-executor"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.order_executor.arn
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


