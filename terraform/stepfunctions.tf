# =============================================================================
# Step Functions: Analysis Orchestration Workflow
# =============================================================================

resource "aws_sfn_state_machine" "analysis_workflow" {
  name     = "${local.name_prefix}-analysis-workflow"
  role_arn = aws_iam_role.step_functions_execution.arn

  definition = jsonencode({
    Comment = "ETH Trading Analysis Orchestration Workflow"
    StartAt = "ParallelAnalysis"
    States = {
      ParallelAnalysis = {
        Type = "Parallel"
        Branches = [
          {
            StartAt = "TechnicalAnalysis"
            States = {
              TechnicalAnalysis = {
                Type     = "Task"
                Resource = "arn:aws:states:::lambda:invoke"
                Parameters = {
                  FunctionName = aws_lambda_function.functions["technical"].arn
                  "Payload.$"  = "$"
                }
                OutputPath = "$.Payload"
                End        = true
              }
            }
          },
          {
            StartAt = "ChronosPrediction"
            States = {
              ChronosPrediction = {
                Type     = "Task"
                Resource = "arn:aws:states:::lambda:invoke"
                Parameters = {
                  FunctionName = aws_lambda_function.functions["chronos-caller"].arn
                  "Payload.$"  = "$"
                }
                OutputPath = "$.Payload"
                End        = true
              }
            }
          },
          {
            StartAt = "SentimentGetter"
            States = {
              SentimentGetter = {
                Type     = "Task"
                Resource = "arn:aws:states:::lambda:invoke"
                Parameters = {
                  FunctionName = aws_lambda_function.functions["sentiment-getter"].arn
                  "Payload.$"  = "$"
                }
                OutputPath = "$.Payload"
                End        = true
              }
            }
          }
        ]
        ResultPath = "$.analysisResults"
        Next       = "MergeResults"
      }

      MergeResults = {
        Type = "Pass"
        Parameters = {
          "pair.$"          = "$.pair"
          "timestamp.$"     = "$.timestamp"
          "current_price.$" = "$.analysisResults[0].current_price"
          "technical.$"     = "$.analysisResults[0]"
          "chronos.$"       = "$.analysisResults[1]"
          "sentiment.$"     = "$.analysisResults[2]"
        }
        Next = "Aggregator"
      }

      Aggregator = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.functions["aggregator"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Next       = "CheckSignal"
      }

      CheckSignal = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.has_signal"
            BooleanEquals = true
            Next          = "SendToOrderQueue"
          }
        ]
        Default = "NoSignal"
      }

      SendToOrderQueue = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage"
        Parameters = {
          QueueUrl     = aws_sqs_queue.order_queue.url
          "MessageBody.$" = "States.JsonToString($)"
        }
        Next = "AnalysisComplete"
      }

      NoSignal = {
        Type   = "Pass"
        Result = { message = "No trading signal generated" }
        End    = true
      }

      AnalysisComplete = {
        Type   = "Pass"
        Result = { message = "Signal sent to order queue" }
        End    = true
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  tags = {
    Name = "${local.name_prefix}-analysis-workflow"
  }
}

# Step Functions用CloudWatch Log Group
resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${local.name_prefix}-analysis-workflow"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-sfn-logs"
  }
}
