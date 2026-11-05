# OCI IPSec 연결 1개 = 상대 쪽 터널 1개.
# 구간마다 이 모듈을 2번 불러 터널 2개(서로 다른 양쪽 IP)를 만든다.
# OCI IPSec 연결은 터널을 늘 2개 만들지만 여기서는 첫 번째 터널만 BGP로 쓴다.

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
  tunnel = data.oci_core_ipsec_connection_tunnels.this.ip_sec_connection_tunnels[0]
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
}

output "oracle_tunnel_ip" {
  description = "상대 쪽에 상대 게이트웨이 IP로 넣는 OCI 터널 공인 IP"
  value       = local.tunnel.vpn_ip
}

output "tunnel_status" {
  value = local.tunnel.status
}
