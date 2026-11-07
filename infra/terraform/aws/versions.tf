terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # 상태는 GCP·Azure 와 같이 HCP Terraform 에 둔다. 실행은 이 PC(백엔드)에서 하는 Local 모드여야 한다 (plan -out 계획 파일 방식).
  cloud {
    organization = "team-alpha"
    workspaces {
      name = "alpha-aws-destination"
    }
  }
}
