# OCI IPSec 연결 1개 = 상대 쪽 터널 1개.
# 구간마다 이 모듈을 2번 불러 터널 2개(서로 다른 양쪽 IP)를 만든다.
# OCI IPSec 연결은 터널을 늘 2개 만들지만 여기서는 tunnel_index가 고른 터널만 BGP로 쓴다.

terraform {
  required_providers {
    oci = {
      source = "oracle/oci"
    }
  }
}

variable "compartment_id" {
  type = string
}

variable "name" {
  type = string
}

variable "drg_id" {
  type = string
}

variable "cpe_ip" {
  description = "상대 VPN 끝점 공인 IP"
  type        = string
}

variable "peer_cidrs" {
  description = "상대 대역. BGP를 쓰더라도 OCI API가 정적 경로 1개 이상을 요구한다"
  type        = list(string)
}

variable "peer_asn" {
  type = string
}

variable "oracle_bgp_ip" {
  description = "터널 안쪽 OCI 주소 (예: 169.254.21.2/30)"
  type        = string
}

variable "peer_bgp_ip" {
  description = "터널 안쪽 상대 주소 (예: 169.254.21.1/30)"
  type        = string
}

variable "shared_secret" {
  type      = string
  sensitive = true
}

variable "phase_two_dh_group" {
  description = "Phase 2 PFS 그룹. null이면 OCI 기본값을 쓴다"
  type        = string
  default     = null
}

variable "tunnel_index" {
  description = "쓸 OCI 공인 IP의 순번(0 또는 1). 같은 구간의 두 연결은 서로 다른 값을 줘야 양쪽 IP가 갈린다"
  type        = number
  default     = 0
}

resource "oci_core_cpe" "this" {
  compartment_id = var.compartment_id
  display_name   = var.name
  ip_address     = var.cpe_ip
}

resource "oci_core_ipsec" "this" {
  compartment_id            = var.compartment_id
  cpe_id                    = oci_core_cpe.this.id
  drg_id                    = var.drg_id
  display_name              = var.name
  static_routes             = var.peer_cidrs
  cpe_local_identifier      = var.cpe_ip
  cpe_local_identifier_type = "IP_ADDRESS"
}

data "oci_core_ipsec_connection_tunnels" "this" {
  ipsec_id = oci_core_ipsec.this.id
}

locals {
  # 연결마다 터널 순서가 달라 목록 위치로는 IP가 겹칠 수 있다. 공인 IP 순서로 고른다.
  tunnels_by_ip = { for t in data.oci_core_ipsec_connection_tunnels.this.ip_sec_connection_tunnels : t.vpn_ip => t }
  tunnel        = local.tunnels_by_ip[sort(keys(local.tunnels_by_ip))[var.tunnel_index]]
}

resource "oci_core_ipsec_connection_tunnel_management" "primary" {
  ipsec_id      = oci_core_ipsec.this.id
  tunnel_id     = local.tunnel.id
  display_name  = "${var.name}-t1"
  routing       = "BGP"
  ike_version   = "V2"
  shared_secret = var.shared_secret

  bgp_session_info {
    customer_bgp_asn      = var.peer_asn
    customer_interface_ip = var.peer_bgp_ip
    oracle_interface_ip   = var.oracle_bgp_ip
  }

  dynamic "phase_two_details" {
    for_each = var.phase_two_dh_group == null ? [] : [var.phase_two_dh_group]
    content {
      is_pfs_enabled = true
      dh_group       = phase_two_details.value
    }
  }
}

output "oracle_tunnel_ip" {
  description = "상대 쪽에 상대 게이트웨이 IP로 넣는 OCI 터널 공인 IP"
  value       = local.tunnel.vpn_ip
}

output "tunnel_status" {
  value = local.tunnel.status
}
