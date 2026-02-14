# =============================================================================
# DynamoDB Tables
# =============================================================================

# 価格データテーブル (TTL: 14日 - テクニカル分析用)
resource "aws_dynamodb_table" "prices" {
  name         = "${local.name_prefix}-prices"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pair"
  range_key    = "timestamp"

  attribute {
    name = "pair"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-prices"
  }
}

# センチメントデータテーブル (TTL: 14日 - ニュース相関分析用)
resource "aws_dynamodb_table" "sentiment" {
  name         = "${local.name_prefix}-sentiment"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pair"
  range_key    = "timestamp"

  attribute {
    name = "pair"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-sentiment"
  }
}

# ポジション・取引テーブルは crypto-order リポジトリに移行
# → https://github.com/kiikun0530/crypto-order

# シグナルテーブル (TTL: 90日 - パフォーマンス分析用)
resource "aws_dynamodb_table" "signals" {
  name         = "${local.name_prefix}-signals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pair"
  range_key    = "timestamp"

  attribute {
    name = "pair"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-signals"
  }
}

# 分析状態管理テーブル
resource "aws_dynamodb_table" "analysis_state" {
  name         = "${local.name_prefix}-analysis-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pair"

  attribute {
    name = "pair"
    type = "S"
  }

  tags = {
    Name = "${local.name_prefix}-analysis-state"
  }
}

# マーケットコンテキストテーブル (TTL: 14日)
# Fear & Greed Index, ファンディングレート, BTC Dominance
resource "aws_dynamodb_table" "market_context" {
  name         = "${local.name_prefix}-market-context"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "context_type"
  range_key    = "timestamp"

  attribute {
    name = "context_type"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-market-context"
  }
}

# TFスコアテーブル (マルチタイムフレーム分析結果)
# PK: pair_tf (e.g., "btc_usdt#1h"), SK: timestamp
# 各TFの分析ワークフローが結果を書き込み、meta-aggregatorが読み取る
resource "aws_dynamodb_table" "tf_scores" {
  name         = "${local.name_prefix}-tf-scores"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pair_tf"
  range_key    = "timestamp"

  attribute {
    name = "pair_tf"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${local.name_prefix}-tf-scores"
  }
}


