# =============================================================================
# SQS Queues
# =============================================================================

# 注文キュー
resource "aws_sqs_queue" "order_queue" {
  name                       = "${local.name_prefix}-order-queue"
  delay_seconds              = 0
  max_message_size           = 262144
  message_retention_seconds  = 86400  # 1日
  receive_wait_time_seconds  = 10     # ロングポーリング
  visibility_timeout_seconds = 60

  # Dead Letter Queue設定
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.order_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name = "${local.name_prefix}-order-queue"
  }
}

# 注文DLQ (Dead Letter Queue)
resource "aws_sqs_queue" "order_dlq" {
  name                      = "${local.name_prefix}-order-dlq"
  message_retention_seconds = 1209600  # 14日

  tags = {
    Name = "${local.name_prefix}-order-dlq"
  }
}

# SQS -> Lambda トリガー
resource "aws_lambda_event_source_mapping" "order_executor_trigger" {
  event_source_arn = aws_sqs_queue.order_queue.arn
  function_name    = aws_lambda_function.functions["order-executor"].arn
  batch_size       = 1
  enabled          = true
}

# SQSへのアクセスポリシー
resource "aws_sqs_queue_policy" "order_queue" {
  queue_url = aws_sqs_queue.order_queue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.order_queue.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sfn_state_machine.analysis_workflow.arn
          }
        }
      }
    ]
  })
}
