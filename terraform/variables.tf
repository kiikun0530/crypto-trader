# =============================================================================
# Variables
# =============================================================================

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "trading_pair" {
  description = "Trading pair (e.g., eth_jpy)"
  type        = string
  default     = "eth_jpy"
}

# ============================================
# Trading Settings
# ============================================
variable "volatility_threshold" {
  description = "Price volatility threshold for triggering analysis (%)"
  type        = number
  default     = 0.3
}

variable "analysis_interval_minutes" {
  description = "Regular analysis interval in minutes"
  type        = number
  default     = 15
}

variable "max_position_jpy" {
  description = "Maximum position size in JPY"
  type        = number
  default     = 15000
}

variable "stop_loss_percent" {
  description = "Stop loss percentage"
  type        = number
  default     = 5
}

variable "take_profit_percent" {
  description = "Take profit percentage"
  type        = number
  default     = 10
}

# ============================================
# DynamoDB TTL Settings
# ============================================
variable "prices_ttl_days" {
  description = "TTL for prices table in days"
  type        = number
  default     = 7
}

variable "sentiment_ttl_days" {
  description = "TTL for sentiment table in days"
  type        = number
  default     = 7
}

variable "signals_ttl_days" {
  description = "TTL for signals table in days"
  type        = number
  default     = 30
}

# ============================================
# Multi-Currency Settings
# ============================================
variable "trading_pairs_config" {
  description = "JSON config for trading pairs (Binance analysis -> Coincheck trading)"
  type        = string
  default     = <<-EOT
{"eth_usdt":{"binance":"ETHUSDT","coincheck":"eth_jpy","news":"ETH","name":"Ethereum"},"btc_usdt":{"binance":"BTCUSDT","coincheck":"btc_jpy","news":"BTC","name":"Bitcoin"},"xrp_usdt":{"binance":"XRPUSDT","coincheck":"xrp_jpy","news":"XRP","name":"XRP"},"sol_usdt":{"binance":"SOLUSDT","coincheck":"sol_jpy","news":"SOL","name":"Solana"},"doge_usdt":{"binance":"DOGEUSDT","coincheck":"doge_jpy","news":"DOGE","name":"Dogecoin"},"avax_usdt":{"binance":"AVAXUSDT","coincheck":"avax_jpy","news":"AVAX","name":"Avalanche"}}
EOT
}

# ============================================
# SageMaker Settings
# ============================================
variable "sagemaker_hf_image_uri" {
  description = "HuggingFace PyTorch Inference DLC image URI for SageMaker"
  type        = string
  default     = "763104351884.dkr.ecr.ap-northeast-1.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.37.0-cpu-py310-ubuntu22.04"
}

# ============================================
# External API Keys
# ============================================
variable "cryptopanic_api_key" {
  description = "CryptoPanic API Key (Growth Plan)"
  type        = string
  sensitive   = true
  default     = ""
}
