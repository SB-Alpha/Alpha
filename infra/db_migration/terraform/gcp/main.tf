# GCP 쪽 VPN (a): 데모 전에 미리 만들어 둔 목적지 VPC에 HA VPN을 붙인다.
# provisioning(infra/terraform/gcp)에서 함께 만들 때는 같은 모듈을 그쪽에서 부른다 (b).

terraform {
  required_version = ">= 1.12"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # 상태는 Object Storage에 실행별로 둔다.
  backend "oci" {}
}

provider "google" {
  project = var.project
  region  = var.region
}

module "vpn" {
  source = "../modules/gcp-vpn"

  project              = var.project
  region               = var.region
  network              = var.network
  name                 = var.name
  oci_tunnels          = var.oci_tunnels
  shared_secrets       = var.shared_secrets
  advertised_ip_ranges = var.advertised_ip_ranges
  export_psa_routes    = var.export_psa_routes
}

output "ha_vpn_interface_ips" {
  value = module.vpn.ha_vpn_interface_ips
}

output "router_asn" {
  value = module.vpn.router_asn
}

output "tunnel_names" {
  value = module.vpn.tunnel_names
}
