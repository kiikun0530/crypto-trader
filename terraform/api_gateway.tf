# =============================================================================
# API Gateway - Signal Publication API
# =============================================================================
# 売買シグナル公開API
# 無料: 30分遅延 / 有料(APIキー): リアルタイム
# =============================================================================

# -----------------------------------------------------------------------------
# API Gateway REST API
# -----------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "signal_api" {
  name        = "${local.name_prefix}-signal-api"
  description = "Crypto Trading AI Signal API - Real-time and delayed signals"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Name = "${local.name_prefix}-signal-api"
  }
}

# -----------------------------------------------------------------------------
# /signals リソース
# -----------------------------------------------------------------------------
resource "aws_api_gateway_resource" "signals" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id
  parent_id   = aws_api_gateway_rest_api.signal_api.root_resource_id
  path_part   = "signals"
}

# /signals/{proxy+} - すべてのサブパスをLambdaに委譲
resource "aws_api_gateway_resource" "signals_proxy" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id
  parent_id   = aws_api_gateway_resource.signals.id
  path_part   = "{proxy+}"
}

# -----------------------------------------------------------------------------
# GET Method
# -----------------------------------------------------------------------------
resource "aws_api_gateway_method" "signals_get" {
  rest_api_id   = aws_api_gateway_rest_api.signal_api.id
  resource_id   = aws_api_gateway_resource.signals_proxy.id
  http_method   = "GET"
  authorization = "NONE"
  # APIキー認証はLambda内で実施（x-api-keyヘッダー）
}

# OPTIONS (CORS preflight)
resource "aws_api_gateway_method" "signals_options" {
  rest_api_id   = aws_api_gateway_rest_api.signal_api.id
  resource_id   = aws_api_gateway_resource.signals_proxy.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

# -----------------------------------------------------------------------------
# Lambda統合
# -----------------------------------------------------------------------------
resource "aws_api_gateway_integration" "signals_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.signal_api.id
  resource_id             = aws_api_gateway_resource.signals_proxy.id
  http_method             = aws_api_gateway_method.signals_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.signal_api.invoke_arn
}

# CORS OPTIONS統合
resource "aws_api_gateway_integration" "signals_options" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id
  resource_id = aws_api_gateway_resource.signals_proxy.id
  http_method = aws_api_gateway_method.signals_options.http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "signals_options_200" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id
  resource_id = aws_api_gateway_resource.signals_proxy.id
  http_method = aws_api_gateway_method.signals_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }

  response_models = {
    "application/json" = "Empty"
  }
}

resource "aws_api_gateway_integration_response" "signals_options" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id
  resource_id = aws_api_gateway_resource.signals_proxy.id
  http_method = aws_api_gateway_method.signals_options.http_method
  status_code = aws_api_gateway_method_response.signals_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Api-Key'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
}

# /signals 直下のGET (redirectなし)
resource "aws_api_gateway_method" "signals_root_get" {
  rest_api_id   = aws_api_gateway_rest_api.signal_api.id
  resource_id   = aws_api_gateway_resource.signals.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "signals_root_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.signal_api.id
  resource_id             = aws_api_gateway_resource.signals.id
  http_method             = aws_api_gateway_method.signals_root_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.signal_api.invoke_arn
}

# -----------------------------------------------------------------------------
# Lambda Function for API
# -----------------------------------------------------------------------------
data "archive_file" "signal_api" {
  type        = "zip"
  source_dir  = "${path.module}/../services/signal-api"
  output_path = "${path.module}/.terraform/tmp/signal-api.zip"
}

resource "aws_lambda_function" "signal_api" {
  function_name = "${local.name_prefix}-signal-api"
  description   = "Signal Publication API (Free: 30min delay, Premium: realtime)"
  role          = aws_iam_role.lambda_execution.arn
  handler       = "handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.signal_api.output_path
  source_code_hash = data.archive_file.signal_api.output_base64sha256

  environment {
    variables = {
      SIGNALS_TABLE        = aws_dynamodb_table.signals.name
      TRADES_TABLE         = aws_dynamodb_table.trades.name
      POSITIONS_TABLE      = aws_dynamodb_table.positions.name
      MARKET_CONTEXT_TABLE = aws_dynamodb_table.market_context.name
      API_KEYS_TABLE       = aws_dynamodb_table.api_keys.name
      FREE_DELAY_SECONDS   = "1800"
      CORS_ORIGIN          = var.signal_api_cors_origin
      TRADING_PAIRS_CONFIG = trimspace(var.trading_pairs_config)
    }
  }

  layers = compact(concat(
    length(aws_lambda_layer_version.common) > 0 ? [aws_lambda_layer_version.common[0].arn] : [],
  ))

  depends_on = [
    aws_iam_role_policy.lambda_custom,
    aws_cloudwatch_log_group.signal_api
  ]

  tags = {
    Name = "${local.name_prefix}-signal-api"
  }
}

resource "aws_cloudwatch_log_group" "signal_api" {
  name              = "/aws/lambda/${local.name_prefix}-signal-api"
  retention_in_days = 14
}

# Lambda呼び出し許可 (API Gateway)
resource "aws_lambda_permission" "signal_api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.signal_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.signal_api.execution_arn}/*/*"
}

# -----------------------------------------------------------------------------
# API Gateway Deployment
# -----------------------------------------------------------------------------
resource "aws_api_gateway_deployment" "signal_api" {
  rest_api_id = aws_api_gateway_rest_api.signal_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.signals.id,
      aws_api_gateway_resource.signals_proxy.id,
      aws_api_gateway_method.signals_get.id,
      aws_api_gateway_method.signals_options.id,
      aws_api_gateway_integration.signals_lambda.id,
      aws_api_gateway_integration.signals_options.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.signals_lambda,
    aws_api_gateway_integration.signals_options,
    aws_api_gateway_integration.signals_root_lambda,
  ]
}

resource "aws_api_gateway_stage" "signal_api" {
  deployment_id = aws_api_gateway_deployment.signal_api.id
  rest_api_id   = aws_api_gateway_rest_api.signal_api.id
  stage_name    = "v1"

  tags = {
    Name = "${local.name_prefix}-signal-api-v1"
  }
}

# -----------------------------------------------------------------------------
# Usage Plan + API Key (レート制限)
# -----------------------------------------------------------------------------
resource "aws_api_gateway_usage_plan" "free" {
  name = "${local.name_prefix}-signal-free"

  api_stages {
    api_id = aws_api_gateway_rest_api.signal_api.id
    stage  = aws_api_gateway_stage.signal_api.stage_name
  }

  throttle_settings {
    burst_limit = 10
    rate_limit  = 5  # 5 req/sec
  }

  quota_settings {
    limit  = 1000
    period = "DAY"
  }
}

resource "aws_api_gateway_usage_plan" "premium" {
  name = "${local.name_prefix}-signal-premium"

  api_stages {
    api_id = aws_api_gateway_rest_api.signal_api.id
    stage  = aws_api_gateway_stage.signal_api.stage_name
  }

  throttle_settings {
    burst_limit = 50
    rate_limit  = 20  # 20 req/sec
  }

  quota_settings {
    limit  = 10000
    period = "DAY"
  }
}

# -----------------------------------------------------------------------------
# DynamoDB - APIキー管理テーブル
# -----------------------------------------------------------------------------
resource "aws_dynamodb_table" "api_keys" {
  name         = "${local.name_prefix}-api-keys"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "api_key"

  attribute {
    name = "api_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-api-keys"
  }
}

# -----------------------------------------------------------------------------
# S3 - フロントエンド静的サイトホスティング
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "signal_frontend" {
  bucket = "${local.name_prefix}-signal-site-${local.account_id}"

  tags = {
    Name = "${local.name_prefix}-signal-site"
  }
}

resource "aws_s3_bucket_website_configuration" "signal_frontend" {
  bucket = aws_s3_bucket.signal_frontend.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "signal_frontend" {
  bucket = aws_s3_bucket.signal_frontend.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "signal_frontend" {
  bucket = aws_s3_bucket.signal_frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.signal_frontend.arn}/*"
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.signal_frontend]
}
