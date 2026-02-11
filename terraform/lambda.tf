# =============================================================================
# Lambda Functions
# =============================================================================

# -----------------------------------------------------------------------------
# CloudWatch Log Groups
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  for_each = toset([
    "price-collector",
    "technical",
    "chronos-caller",
    "sentiment-getter",
    "aggregator",
    "order-executor",
    "position-monitor",
    "news-collector",
    "market-context",
    "warm-up"
  ])

  name              = "/aws/lambda/${local.name_prefix}-${each.key}"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-${each.key}"
  }
}

# -----------------------------------------------------------------------------
# Lambda Layer (共通依存関係)
# -----------------------------------------------------------------------------
data "archive_file" "lambda_layer" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda_layer"
  output_path = "${path.module}/.terraform/tmp/lambda_layer.zip"
}

resource "aws_lambda_layer_version" "common" {
  count               = fileexists("${path.module}/../lambda_layer/python/requirements.txt") ? 1 : 0
  filename            = data.archive_file.lambda_layer.output_path
  source_code_hash    = data.archive_file.lambda_layer.output_base64sha256
  layer_name          = "${local.name_prefix}-common"
  compatible_runtimes = ["python3.11", "python3.12"]
}

# -----------------------------------------------------------------------------
# Lambda Functions
# -----------------------------------------------------------------------------

locals {
  lambda_functions = {
    price-collector = {
      description = "価格データ収集（全通貨）"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    technical = {
      description = "テクニカル分析"
      timeout     = 60
      memory      = 512
      handler     = "handler.handler"
    }
    chronos-caller = {
      description = "Chronos-2 AI予測 (SageMaker Serverless)"
      timeout     = 180
      memory      = 256
      handler     = "handler.handler"
    }
    sentiment-getter = {
      description = "センチメント分析"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    aggregator = {
      description = "分析結果集約（全通貨比較）"
      timeout     = 120
      memory      = 512
      handler     = "handler.handler"
    }
    order-executor = {
      description = "注文実行"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    position-monitor = {
      description = "ポジション監視（全通貨）"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    news-collector = {
      description = "ニュース収集"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    market-context = {
      description = "マーケットコンテキスト収集 (F&G, Funding, BTC Dom)"
      timeout     = 60
      memory      = 256
      handler     = "handler.handler"
    }
    warm-up = {
      description = "初回データ投入（手動実行）"
      timeout     = 300
      memory      = 512
      handler     = "handler.handler"
    }
  }

  lambda_environment = {
    PRICES_TABLE         = aws_dynamodb_table.prices.name
    SENTIMENT_TABLE      = aws_dynamodb_table.sentiment.name
    POSITIONS_TABLE      = aws_dynamodb_table.positions.name
    TRADES_TABLE         = aws_dynamodb_table.trades.name
    SIGNALS_TABLE        = aws_dynamodb_table.signals.name
    ANALYSIS_STATE_TABLE = aws_dynamodb_table.analysis_state.name
    MARKET_CONTEXT_TABLE = aws_dynamodb_table.market_context.name
    COINCHECK_SECRET_ARN = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:coincheck/api-credentials"
    MAX_POSITION_JPY     = tostring(var.max_position_jpy)
    CRYPTOPANIC_API_KEY  = var.cryptopanic_api_key
    ORDER_QUEUE_URL      = "https://sqs.${var.aws_region}.amazonaws.com/${local.account_id}/${local.name_prefix}-order-queue"
    SLACK_WEBHOOK_URL      = var.slack_webhook_url
    TRADING_PAIRS_CONFIG   = trimspace(var.trading_pairs_config)
    MODEL_BUCKET           = "${local.name_prefix}-sagemaker-models-${local.account_id}"
    MODEL_PREFIX           = "chronos-onnx"
    SAGEMAKER_ENDPOINT     = "${local.name_prefix}-chronos-base"
    BEDROCK_MODEL_ID       = "us.amazon.nova-micro-v1:0"
  }
}

# Lambda関数用ZIPファイル作成
data "archive_file" "lambda" {
  for_each = local.lambda_functions

  type        = "zip"
  source_dir  = "${path.module}/../services/${each.key}"
  output_path = "${path.module}/.terraform/tmp/${each.key}.zip"
}

# Lambda関数
resource "aws_lambda_function" "functions" {
  for_each = local.lambda_functions

  function_name = "${local.name_prefix}-${each.key}"
  description   = each.value.description
  role          = aws_iam_role.lambda_execution.arn
  handler       = each.value.handler
  runtime       = "python3.11"
  timeout       = each.value.timeout
  memory_size   = each.value.memory

  filename         = data.archive_file.lambda[each.key].output_path
  source_code_hash = data.archive_file.lambda[each.key].output_base64sha256

  # VPC外で実行 (コスト削減: NAT Gateway $45/月を節約)
  # DynamoDB/S3/Secrets Managerはパブリックエンドポイント経由でアクセス (IAM認証あり)

  environment {
    variables = local.lambda_environment
  }

  # Lambda Layer設定 (共通レイヤーのみ。chronos-callerはSageMaker経由のため ONNX Runtime Layer 不要)
  layers = compact(concat(
    length(aws_lambda_layer_version.common) > 0 ? [aws_lambda_layer_version.common[0].arn] : [],
  ))

  depends_on = [
    aws_iam_role_policy.lambda_custom,
    aws_cloudwatch_log_group.lambda
  ]

  tags = {
    Name = "${local.name_prefix}-${each.key}"
  }
}

# -----------------------------------------------------------------------------
# Lambda Permissions (Step Functionsからの呼び出し許可)
# -----------------------------------------------------------------------------
resource "aws_lambda_permission" "step_functions" {
  for_each = local.lambda_functions

  statement_id  = "AllowStepFunctionsInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions[each.key].function_name
  principal     = "states.amazonaws.com"
  source_arn    = aws_sfn_state_machine.analysis_workflow.arn
}
