# =============================================================================
# Outputs
# =============================================================================

# -----------------------------------------------------------------------------
# DynamoDB Outputs
# -----------------------------------------------------------------------------
output "dynamodb_tables" {
  description = "DynamoDB table names"
  value = {
    prices         = aws_dynamodb_table.prices.name
    sentiment      = aws_dynamodb_table.sentiment.name
    positions      = aws_dynamodb_table.positions.name
    trades         = aws_dynamodb_table.trades.name
    signals        = aws_dynamodb_table.signals.name
    analysis_state = aws_dynamodb_table.analysis_state.name
  }
}

# -----------------------------------------------------------------------------
# Lambda Outputs
# -----------------------------------------------------------------------------
output "lambda_functions" {
  description = "Lambda function names"
  value       = { for k, v in aws_lambda_function.functions : k => v.function_name }
}

output "lambda_arns" {
  description = "Lambda function ARNs"
  value       = { for k, v in aws_lambda_function.functions : k => v.arn }
}

# -----------------------------------------------------------------------------
# Step Functions Outputs
# -----------------------------------------------------------------------------
output "analysis_workflow_arn" {
  description = "Analysis workflow state machine ARN"
  value       = aws_sfn_state_machine.analysis_workflow.arn
}

output "analysis_workflow_name" {
  description = "Analysis workflow state machine name"
  value       = aws_sfn_state_machine.analysis_workflow.name
}

# -----------------------------------------------------------------------------
# SQS Outputs
# -----------------------------------------------------------------------------
output "order_queue_url" {
  description = "Order queue URL"
  value       = aws_sqs_queue.order_queue.url
}

output "order_queue_arn" {
  description = "Order queue ARN"
  value       = aws_sqs_queue.order_queue.arn
}

# -----------------------------------------------------------------------------
# SNS Outputs
# -----------------------------------------------------------------------------
output "notifications_topic_arn" {
  description = "Notifications SNS topic ARN"
  value       = aws_sns_topic.notifications.arn
}

output "alerts_topic_arn" {
  description = "Alerts SNS topic ARN"
  value       = aws_sns_topic.alerts.arn
}

# -----------------------------------------------------------------------------
# Signal API Outputs
# -----------------------------------------------------------------------------
output "signal_api_url" {
  description = "Signal API Gateway URL"
  value       = "${aws_api_gateway_stage.signal_api.invoke_url}/signals"
}

output "signal_api_id" {
  description = "Signal API Gateway ID"
  value       = aws_api_gateway_rest_api.signal_api.id
}

output "signal_frontend_bucket" {
  description = "Signal frontend S3 bucket name"
  value       = aws_s3_bucket.signal_frontend.bucket
}

output "signal_frontend_url" {
  description = "Signal frontend website URL"
  value       = aws_s3_bucket_website_configuration.signal_frontend.website_endpoint
}

# -----------------------------------------------------------------------------
# IAM Outputs
# -----------------------------------------------------------------------------
output "lambda_execution_role_arn" {
  description = "Lambda execution role ARN"
  value       = aws_iam_role.lambda_execution.arn
}

output "step_functions_role_arn" {
  description = "Step Functions execution role ARN"
  value       = aws_iam_role.step_functions_execution.arn
}
