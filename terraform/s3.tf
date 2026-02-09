# =============================================================================
# S3 Buckets
# =============================================================================

# -----------------------------------------------------------------------------
# ONNXモデル格納 (Chronos-T5-Tiny)
# 旧: SageMaker model artifacts → 現: ONNX Runtime用モデルファイル
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
# 日次レポート格納 (Daily Reporter)
# 90日間保持後自動削除
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "daily_reports" {
  bucket = "${local.name_prefix}-daily-reports-${local.account_id}"

  tags = {
    Name = "${local.name_prefix}-daily-reports"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "daily_reports" {
  bucket = aws_s3_bucket.daily_reports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "daily_reports" {
  bucket = aws_s3_bucket.daily_reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "daily_reports" {
  bucket = aws_s3_bucket.daily_reports.id

  rule {
    id     = "expire-old-reports"
    status = "Enabled"

    filter {
      prefix = "daily-reports/"
    }

    expiration {
      days = 90
    }
  }
}
