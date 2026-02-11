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
variable "volatility_threshold" {
  description = "Price volatility threshold for triggering analysis (%)"
  type        = number
  default     = 0.3
}

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
{"eth_usdt":{"binance":"ETHUSDT","coincheck":"eth_jpy","news":"ETH","name":"Ethereum"},"btc_usdt":{"binance":"BTCUSDT","coincheck":"btc_jpy","news":"BTC","name":"Bitcoin"},"xrp_usdt":{"binance":"XRPUSDT","coincheck":"xrp_jpy","news":"XRP","name":"XRP"},"sol_usdt":{"binance":"SOLUSDT","coincheck":"sol_jpy","news":"SOL","name":"Solana"},"doge_usdt":{"binance":"DOGEUSDT","coincheck":"doge_jpy","news":"DOGE","name":"Dogecoin"},"avax_usdt":{"binance":"AVAXUSDT","coincheck":"avax_jpy","news":"AVAX","name":"Avalanche"}}
EOT
}

# ============================================
# Signal API Settings
# ============================================
variable "signal_api_cors_origin" {
  description = "CORS origin for Signal API (e.g. https://your-domain.com or * for dev)"
  type        = string
  default     = "*"
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
