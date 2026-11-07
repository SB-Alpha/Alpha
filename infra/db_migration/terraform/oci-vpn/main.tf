# OCI 쪽 VPN만 따로 둔 루트. 이전 실행(run)마다 만들고 지우므로 노드·네트워크(oci/)와 상태를 나눈다.
# 상태는 Object Storage에 실행별로 둔다: terraform init -backend-config=...

terraform {
  required_version = ">= 1.12"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 7.0"
    }
  }

  backend "oci" {}
}

provider "oci" {
  region              = var.region
  config_file_profile = var.config_profile
}

module "vpn" {
  source   = "../modules/oci-vpn-peer"
  for_each = var.tunnels

  compartment_id = var.compartment_id
  name           = "${var.name}-${each.key}"
  drg_id         = var.drg_id
  cpe_ip         = each.value.cpe_ip
  peer_cidrs     = each.value.peer_cidrs
  peer_asn       = each.value.peer_asn
  oracle_bgp_ip  = each.value.oracle_bgp_ip
  peer_bgp_ip    = each.value.peer_bgp_ip
  shared_secret  = var.shared_secrets[each.key]
  # 키 끝 번호(source-0, source-1)로 구간 안에서 IP를 나눈다.
  tunnel_index = tonumber(regex("[01]$", each.key))

  phase_two_dh_group = each.value.phase_two_dh_group
}

output "oracle_tunnel_ips" {
  description = "상대 구간에 넘길 OCI 터널 공인 IP. 입력의 터널 키를 그대로 유지한다"
  value       = { for k, v in module.vpn : k => v.oracle_tunnel_ip }
}

output "tunnel_status" {
  value = { for k, v in module.vpn : k => v.tunnel_status }
}
