# =============================================================================
# IAM Roles and Policies
# =============================================================================

# -----------------------------------------------------------------------------
# Lambda用IAMロール
# -----------------------------------------------------------------------------
resource "aws_iam_role" "lambda_execution" {
  name = "${local.name_prefix}-lambda-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.name_prefix}-lambda-execution"
  }
}

# Lambda基本実行ポリシー (CloudWatch Logs)
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Lambda VPCアクセスポリシー - VPC外実行のため不要
# resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
#   role       = aws_iam_role.lambda_execution.name
#   policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
# }

# Lambda用カスタムポリシー (DynamoDB, S3, Secrets Manager, SQS, SNS)
resource "aws_iam_role_policy" "lambda_custom" {
  name = "${local.name_prefix}-lambda-custom"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB フルアクセス (テーブル限定)
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem"
        ]
        Resource = [
          aws_dynamodb_table.prices.arn,
          aws_dynamodb_table.sentiment.arn,
          aws_dynamodb_table.positions.arn,
          aws_dynamodb_table.trades.arn,
          aws_dynamodb_table.signals.arn,
          aws_dynamodb_table.analysis_state.arn,
          "${aws_dynamodb_table.prices.arn}/index/*",
          "${aws_dynamodb_table.sentiment.arn}/index/*",
          "${aws_dynamodb_table.positions.arn}/index/*",
          "${aws_dynamodb_table.trades.arn}/index/*",
          "${aws_dynamodb_table.signals.arn}/index/*",
          "${aws_dynamodb_table.analysis_state.arn}/index/*"
        ]
      },
      # Secrets Managerアクセス
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:coincheck/*",
          "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:github/*"
        ]
      },
      # SQSアクセス
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = [
          "arn:aws:sqs:${var.aws_region}:${local.account_id}:${local.name_prefix}-*"
        ]
      },
      # SNSアクセス
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = [
          "arn:aws:sns:${var.aws_region}:${local.account_id}:${local.name_prefix}-*"
        ]
      },
      # Step Functions実行
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution"
        ]
        Resource = [
          "arn:aws:states:${var.aws_region}:${local.account_id}:stateMachine:${local.name_prefix}-*"
        ]
      },
      # S3: ONNXモデル読み取り (Chronos AI価格予測)
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${local.name_prefix}-sagemaker-models-${local.account_id}",
          "arn:aws:s3:::${local.name_prefix}-sagemaker-models-${local.account_id}/*"
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Step Functions用IAMロール
# -----------------------------------------------------------------------------
resource "aws_iam_role" "step_functions_execution" {
  name = "${local.name_prefix}-sfn-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.name_prefix}-sfn-execution"
  }
}

# Step Functions用カスタムポリシー
resource "aws_iam_role_policy" "step_functions_custom" {
  name = "${local.name_prefix}-sfn-custom"
  role = aws_iam_role.step_functions_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Lambda呼び出し
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = [
          "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${local.name_prefix}-*"
        ]
      },
      # CloudWatch Logs
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutLogEvents",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups"
        ]
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# EventBridge用IAMロール
# -----------------------------------------------------------------------------
resource "aws_iam_role" "eventbridge_execution" {
  name = "${local.name_prefix}-eventbridge-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.name_prefix}-eventbridge-execution"
  }
}

# EventBridge用ポリシー (Step Functions起動)
resource "aws_iam_role_policy" "eventbridge_sfn" {
  name = "${local.name_prefix}-eventbridge-sfn"
  role = aws_iam_role.eventbridge_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution"
        ]
        Resource = [
          "arn:aws:states:${var.aws_region}:${local.account_id}:stateMachine:${local.name_prefix}-*"
        ]
      }
    ]
  })
}
