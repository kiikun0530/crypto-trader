# =============================================================================
# Step Functions: Multi-Timeframe Analysis Workflow
# =============================================================================
# パラメータ化されたワークフロー: EventBridgeから timeframe を受け取り、
# 価格収集 → テクニカル+センチメント(並列) → Chronos(直列) → TFスコア保存
# の4フェーズで分析を実行する。
#
# 同一のState Machineを全TF (15m, 1h, 4h, 1d) で共用。
# EventBridgeが各TFスケジュールごとに timeframe パラメータを変えて起動する。
# =============================================================================

resource "aws_sfn_state_machine" "analysis_workflow" {
  name     = "${local.name_prefix}-analysis-workflow"
  role_arn = aws_iam_role.step_functions_execution.arn

  definition = jsonencode({
    Comment = "Multi-Timeframe Trading Analysis Workflow"
    StartAt = "CollectPrices"
    States = {

      # Phase 1: 価格収集（全通貨・指定TF）
      CollectPrices = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.functions["price-collector"].arn
          Payload = {
            "timeframe.$" = "$.timeframe"
            "pairs.$"     = "$.pairs"
          }
        }
        ResultPath = "$.price_result"
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
            ResultPath  = "$.priceError"
            Next        = "AnalysisFailed"
          }
        ]
        Next = "AnalyzeTechSentiment"
      }

      # Phase 2: テクニカル + センチメント並列分析 (MaxConcurrency=3)
      AnalyzeTechSentiment = {
        Type           = "Map"
        MaxConcurrency = 3
        ItemsPath      = "$.pairs"
        ItemSelector = {
          "pair.$"      = "$$.Map.Item.Value"
          "timeframe.$" = "$.timeframe"
        }
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "TechSentParallel"
          States = {
            TechSentParallel = {
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
              Next       = "MergeTechSent"
              Catch = [
                {
                  ErrorEquals = ["States.ALL"]
                  ResultPath  = "$.analysisError"
                  Next        = "MergeTechSent"
                }
              ]
            }

            MergeTechSent = {
              Type = "Pass"
              Parameters = {
                "pair.$"      = "$.pair"
                "timeframe.$" = "$.timeframe"
                "technical.$"  = "$.analysisResults[0]"
                "sentiment.$"  = "$.analysisResults[1]"
              }
              End = true
            }
          }
        }
        ResultPath = "$.tech_sent_results"
        Next       = "ChronosSequential"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.techSentError"
            Next        = "AnalysisFailed"
          }
        ]
      }

      # Phase 3: Chronos AI予測 (MaxConcurrency=1 — SageMaker同時実行制限対策)
      ChronosSequential = {
        Type           = "Map"
        MaxConcurrency = 1
        ItemsPath      = "$.pairs"
        ItemSelector = {
          "pair.$"      = "$$.Map.Item.Value"
          "timeframe.$" = "$.timeframe"
        }
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "ChronosCaller"
          States = {
            ChronosCaller = {
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
                  IntervalSeconds = 3
                  MaxAttempts     = 3
                  BackoffRate     = 2
                }
              ]
              End = true
            }
          }
        }
        ResultPath = "$.chronos_results"
        Next       = "SaveTFScores"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.chronosError"
            Next        = "AnalysisFailed"
          }
        ]
      }

      # Phase 4: TFスコア保存 (aggregator tf_score mode)
      SaveTFScores = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.functions["aggregator"].arn
          Payload = {
            "mode"               = "tf_score"
            "timeframe.$"        = "$.timeframe"
            "pairs.$"            = "$.pairs"
            "tech_sent_results.$" = "$.tech_sent_results"
            "chronos_results.$"   = "$.chronos_results"
          }
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
        Result = { message = "Multi-TF analysis complete" }
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
