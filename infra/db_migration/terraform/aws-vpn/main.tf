# AWS 쪽 VPN (원본 VPC에 VGW·VPN 연결, RDS 경로 전파, RDS 보안 그룹 규칙).
# 원본 계정 권한이 필요하다. 권한을 받지 못하면 이 루트를 원본 담당자가 apply한다.

terraform {
  required_version = ">= 1.12"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # 상태는 Object Storage에 실행별로 둔다.
  backend "oci" {}
}

provider "aws" {
  region  = var.region
  profile = var.profile
}

module "vpn" {
  source = "../modules/aws-vpn"

  name                 = var.name
  vpc_id               = var.vpc_id
  route_table_ids      = var.route_table_ids
  db_security_group_id = var.db_security_group_id
  oci_cidr             = var.oci_cidr
  tunnels              = var.tunnels
  shared_secrets       = var.shared_secrets
}

output "tunnels" {
  value = module.vpn.tunnels
}
