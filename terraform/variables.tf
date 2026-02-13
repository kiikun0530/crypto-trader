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
  description = "Trading pair (e.g., eth_jpy) [DEPRECATED - use trading_pairs_config]"
  type        = string
  default     = "eth_jpy"
}

# ============================================
# Trading Settings
# ============================================
variable "max_position_jpy" {
  description = "Maximum position size in JPY"
  type        = number
  default     = 15000
}

# ============================================
# Multi-Currency Settings
# ============================================
variable "trading_pairs_config" {
  description = "JSON config for trading pairs (Binance analysis -> Coincheck trading)"
  type        = string
  default     = <<-EOT
{"btc_usdt":{"binance":"BTCUSDT","coincheck":"btc_jpy","news":"BTC","name":"Bitcoin"},"eth_usdt":{"binance":"ETHUSDT","coincheck":"eth_jpy","news":"ETH","name":"Ethereum"},"xrp_usdt":{"binance":"XRPUSDT","coincheck":"xrp_jpy","news":"XRP","name":"XRP"}}
EOT
}

# (SageMaker設定は削除 — ONNX Runtime Lambda移行済み)

# ============================================
# External API Keys
# ============================================
variable "cryptopanic_api_key" {
  description = "CryptoPanic API Key (Growth Plan)"
  type        = string
  sensitive   = true
  default     = ""
}
