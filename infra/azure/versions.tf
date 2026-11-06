terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }

  # Alpha 는 사용자 PC 에서 도는 앱이라 상태도 PC 에 둔다. *.tfstate 는 커밋되지 않는다.
  backend "local" {}
}
