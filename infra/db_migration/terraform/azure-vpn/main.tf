# Azure 쪽 VPN (DB가 있는 VNet에 VPN Gateway·OCI 연결).
# 인증은 환경변수 ARM_TENANT_ID, ARM_CLIENT_ID, ARM_CLIENT_SECRET, ARM_SUBSCRIPTION_ID로 받는다.

terraform {
  required_version = ">= 1.12"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.13"
    }
  }

  # 상태는 Object Storage에 실행별로 둔다.
  backend "oci" {}
}

provider "azurerm" {
  features {}
}

module "vpn" {
  source = "../modules/azure-vpn"

  resource_group  = var.resource_group
  location        = var.location
  vnet_name       = var.vnet_name
  name            = var.name
  sku             = var.sku
  gateway_asn     = var.gateway_asn
  gateway_bgp_ips = var.gateway_bgp_ips
  oci_tunnels     = var.oci_tunnels
  shared_secrets  = var.shared_secrets
}

output "gateway_public_ips" {
  value = module.vpn.gateway_public_ips
}

output "gateway_asn" {
  value = module.vpn.gateway_asn
}

output "gateway_bgp_ips" {
  value = module.vpn.gateway_bgp_ips
}
