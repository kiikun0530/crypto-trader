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
