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
