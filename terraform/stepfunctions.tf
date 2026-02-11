# =============================================================================
# Step Functions: Analysis Orchestration Workflow
# =============================================================================

resource "aws_sfn_state_machine" "analysis_workflow" {
  name     = "${local.name_prefix}-analysis-workflow"
  role_arn = aws_iam_role.step_functions_execution.arn

  definition = jsonencode({
    Comment = "Multi-Currency Trading Analysis Workflow"
    StartAt = "AnalyzeAllPairs"
    States = {
      # Map: 全通貨ペアを並列分析 (クォータ10に対して6ペア並列)
      AnalyzeAllPairs = {
        Type           = "Map"
        MaxConcurrency = 6
        ItemsPath      = "$.pairs"
        ItemSelector = {
          "pair.$"      = "$$.Map.Item.Value"
          "timestamp.$" = "$.timestamp"
        }
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
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
                      Retry = [
                        {
                          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
                          IntervalSeconds = 2
                          MaxAttempts     = 3
                          BackoffRate     = 2
                        }
                      ]
                      End = true
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
                      Retry = [
                        {
                          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
                          IntervalSeconds = 2
                          MaxAttempts     = 3
                          BackoffRate     = 2
                        }
                      ]
                      End = true
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
                      Retry = [
                        {
                          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
                          IntervalSeconds = 2
                          MaxAttempts     = 3
                          BackoffRate     = 2
                        }
                      ]
                      End = true
                    }
                  }
                }
              ]
              ResultPath = "$.analysisResults"
              Next       = "MergePairResults"
              Catch = [
                {
                  ErrorEquals  = ["States.ALL"]
                  ResultPath   = "$.analysisError"
                  Next         = "MergePairResults"
                }
              ]
            }

            MergePairResults = {
              Type = "Pass"
              Parameters = {
                "pair.$"      = "$.pair"
                "timestamp.$" = "$.timestamp"
                "technical.$"  = "$.analysisResults[0]"
                "chronos.$"    = "$.analysisResults[1]"
                "sentiment.$"  = "$.analysisResults[2]"
              }
              End = true
            }
          }
        }
        ResultPath = "$.analysis_results"
        Next       = "Aggregator"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.mapError"
            Next        = "AnalysisFailed"
          }
        ]
      }

      # 全通貨のスコア比較 + 最適通貨選定
      Aggregator = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.functions["aggregator"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
            IntervalSeconds = 2
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error"
            Next        = "AnalysisFailed"
          }
        ]
        Next = "AnalysisComplete"
      }

      AnalysisComplete = {
        Type   = "Pass"
        Result = { message = "Multi-currency analysis complete" }
        End    = true
      }

      AnalysisFailed = {
        Type   = "Pass"
        Result = { message = "Analysis workflow failed" }
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
