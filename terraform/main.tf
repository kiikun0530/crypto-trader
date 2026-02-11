# =============================================================================
# ETH Trading Bot - Terraform Configuration
# =============================================================================
# このコードでarch.txtの全インフラをゼロから構築できます
# =============================================================================

terraform {
  required_version = ">= 1.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  backend "s3" {
    bucket       = "eth-trading-terraform-state-652679684315"
    key          = "terraform.tfstate"
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "eth-trading-bot"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ローカル変数
locals {
  name_prefix = "eth-trading"
  account_id  = data.aws_caller_identity.current.account_id
}

# 現在のAWSアカウント情報
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
