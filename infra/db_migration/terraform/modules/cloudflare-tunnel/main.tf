# CDC 제어 에이전트 입구: Cloudflare Tunnel + Access 서비스 토큰.
# 노드마다 cloudflared가 같은 Tunnel 토큰으로 붙어, 1대가 멈춰도 나머지로 요청이 간다.
# 토큰이 없는 요청은 Access에서 막혀 에이전트까지 오지 않는다.

terraform {
  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}

variable "account_id" {
  type = string
}

variable "zone_id" {
  type = string
}

variable "zone_name" {
  description = "도메인 (예: example.com)"
  type        = string
}

variable "subdomain" {
  type    = string
  default = "cdc"
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "origin_service" {
  description = "cloudflared가 같은 노드에서 넘겨줄 에이전트 주소"
  type        = string
  default     = "http://localhost:8080"
}

locals {
  hostname = "${var.subdomain}.${var.zone_name}"
}

resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.account_id
  name       = var.name
  secret     = random_id.tunnel_secret.b64_std
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config {
    ingress_rule {
      hostname = local.hostname
      service  = var.origin_service
    }

    ingress_rule {
      service = "http_status:404"
    }
  }
}

resource "cloudflare_record" "this" {
  zone_id = var.zone_id
  name    = var.subdomain
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  type    = "CNAME"
  proxied = true
}

resource "cloudflare_zero_trust_access_application" "this" {
  zone_id          = var.zone_id
  name             = var.name
  domain           = local.hostname
  type             = "self_hosted"
  session_duration = "24h"
}

resource "cloudflare_zero_trust_access_service_token" "backend" {
  account_id = var.account_id
  name       = "${var.name}-backend"
}

resource "cloudflare_zero_trust_access_policy" "backend" {
  zone_id        = var.zone_id
  application_id = cloudflare_zero_trust_access_application.this.id
  name           = "backend service token"
  precedence     = 1
  decision       = "non_identity"

  include {
    service_token = [cloudflare_zero_trust_access_service_token.backend.id]
  }
}

output "hostname" {
  value = local.hostname
}

output "tunnel_token" {
  description = "노드의 cloudflared 실행 토큰. OCI Vault에 넣고 Git·로그에 남기지 않는다"
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.tunnel_token
  sensitive   = true
}

output "access_client_id" {
  value = cloudflare_zero_trust_access_service_token.backend.client_id
}

output "access_client_secret" {
  description = "backend .env의 CF-Access-Client-Secret"
  value       = cloudflare_zero_trust_access_service_token.backend.client_secret
  sensitive   = true
}
