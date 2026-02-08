# =============================================================================
# SageMaker Serverless Inference — Chronos-T5-Tiny (AI価格予測)
# =============================================================================
# スモールスタート: Serverless Inference で使った分だけ課金
# 月額概算: ~$3-8 (5分間隔 × 6通貨)
# =============================================================================

# -----------------------------------------------------------------------------
# S3: モデルアーティファクト格納
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "sagemaker_models" {
  bucket = "${local.name_prefix}-sagemaker-models-${local.account_id}"

  tags = {
    Name = "${local.name_prefix}-sagemaker-models"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sagemaker_models" {
  bucket = aws_s3_bucket.sagemaker_models.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "sagemaker_models" {
  bucket = aws_s3_bucket.sagemaker_models.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# モデルアーティファクト (model.tar.gz) の作成 & S3アップロード
# code/inference.py + code/requirements.txt を tar.gz にパッケージ
# -----------------------------------------------------------------------------
resource "null_resource" "chronos_model_package" {
  triggers = {
    inference_hash    = filesha256("${path.module}/../sagemaker/chronos-tiny/code/inference.py")
    requirements_hash = filesha256("${path.module}/../sagemaker/chronos-tiny/code/requirements.txt")
  }

  provisioner "local-exec" {
    command = "python ${path.module}/../sagemaker/package_model.py ${path.module}/.terraform/tmp/chronos-model.tar.gz"
  }
}

resource "aws_s3_object" "chronos_model" {
  bucket = aws_s3_bucket.sagemaker_models.id
  key    = "chronos-tiny/model.tar.gz"
  source = "${path.module}/.terraform/tmp/chronos-model.tar.gz"
  etag   = filemd5("${path.module}/../sagemaker/chronos-tiny/code/inference.py")

  depends_on = [null_resource.chronos_model_package]
}

# -----------------------------------------------------------------------------
# IAM Role: SageMaker 実行ロール
# -----------------------------------------------------------------------------
resource "aws_iam_role" "sagemaker_execution" {
  name = "${local.name_prefix}-sagemaker-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "sagemaker.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.name_prefix}-sagemaker-execution"
  }
}

resource "aws_iam_role_policy" "sagemaker_custom" {
  name = "${local.name_prefix}-sagemaker-custom"
  role = aws_iam_role.sagemaker_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3: モデルアーティファクト読み取り
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.sagemaker_models.arn,
          "${aws_s3_bucket.sagemaker_models.arn}/*"
        ]
      },
      # ECR: コンテナイメージ取得
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
        ]
        Resource = "*"
      },
      # CloudWatch Logs: 推論ログ
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# SageMaker Model
# -----------------------------------------------------------------------------
resource "aws_sagemaker_model" "chronos" {
  name               = "${local.name_prefix}-chronos-tiny"
  execution_role_arn = aws_iam_role.sagemaker_execution.arn

  primary_container {
    # HuggingFace PyTorch Inference DLC (CPU, ap-northeast-1)
    image          = var.sagemaker_hf_image_uri
    model_data_url = "s3://${aws_s3_bucket.sagemaker_models.id}/chronos-tiny/model.tar.gz"
    environment = {
      SAGEMAKER_PROGRAM = "inference.py"
    }
  }

  depends_on = [aws_s3_object.chronos_model]

  tags = {
    Name = "${local.name_prefix}-chronos-tiny"
  }
}

# -----------------------------------------------------------------------------
# SageMaker Endpoint Configuration (Serverless)
# -----------------------------------------------------------------------------
resource "aws_sagemaker_endpoint_configuration" "chronos" {
  name = "${local.name_prefix}-chronos-tiny"

  production_variants {
    variant_name           = "default"
    model_name             = aws_sagemaker_model.chronos.name

    serverless_config {
      max_concurrency   = 5
      memory_size_in_mb = 4096
    }
  }

  tags = {
    Name = "${local.name_prefix}-chronos-tiny"
  }
}

# -----------------------------------------------------------------------------
# SageMaker Endpoint
# -----------------------------------------------------------------------------
resource "aws_sagemaker_endpoint" "chronos" {
  name                 = "${local.name_prefix}-chronos-tiny"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.chronos.name

  tags = {
    Name = "${local.name_prefix}-chronos-tiny"
  }
}
