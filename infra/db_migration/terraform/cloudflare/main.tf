# CDC 제어 입구: Tunnel + DNS + Access 서비스 토큰.
# API 토큰은 CLOUDFLARE_API_TOKEN 환경 변수로 준다.

terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.52"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "cloudflare" {}

module "tunnel" {
  source = "../modules/cloudflare-tunnel"

  account_id = var.account_id
  zone_id    = var.zone_id
  zone_name  = var.zone_name
  subdomain  = var.subdomain
}

output "hostname" {
  value = module.tunnel.hostname
}

output "tunnel_token" {
  value     = module.tunnel.tunnel_token
  sensitive = true
}

output "access_client_id" {
  value = module.tunnel.access_client_id
}

output "access_client_secret" {
  value     = module.tunnel.access_client_secret
  sensitive = true
}
