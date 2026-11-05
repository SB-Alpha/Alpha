# GCP 쪽 VPN: HA VPN 게이트웨이(인터페이스 2개) + Cloud Router + OCI 터널 2개.
# (a) infra/db_migration/terraform/gcp 와 (b) infra/terraform/gcp 가 같은 모듈을 부른다.
#
# 만드는 순서 (OCI와 서로의 IP가 필요하다):
#   1. oci_tunnels = [] 로 apply → ha_vpn_interface_ips 확보
#   2. OCI 쪽 apply (cpe_ip = 위 IP) → OCI 터널 IP 확보
#   3. oci_tunnels 를 채워 다시 apply → 외부 게이트웨이·터널·BGP 생성

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "network" {
  description = "Cloud SQL이 사설 IP로 붙어 있는 VPC 이름"
  type        = string
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "router_asn" {
  type    = number
  default = 65010
}

variable "oci_asn" {
  type    = number
  default = 31898
}

variable "oci_tunnels" {
  description = "HA VPN 인터페이스 0, 1 순서. oci_ip는 OCI 쪽 oracle_tunnel_ip 출력값"
  type = list(object({
    oci_ip     = string
    gcp_bgp_ip = string # 169.254.31.1/30 처럼 접두 길이 포함
    oci_bgp_ip = string # 169.254.31.2 (접두 길이 없이)
  }))
  default = []

  validation {
    condition     = contains([0, 2], length(var.oci_tunnels))
    error_message = "oci_tunnels는 비어 있거나(1단계) 2개다."
  }
}

variable "shared_secrets" {
  description = "oci_tunnels와 같은 순서. OCI 쪽과 같은 값"
  type        = list(string)
  default     = []
  sensitive   = true
}

variable "advertised_ip_ranges" {
  description = "OCI로 알릴 대역. Cloud SQL PSA 할당 대역을 넣는다 (서브넷은 자동으로 알린다)"
  type        = list(string)
  default     = []
}

variable "export_psa_routes" {
  description = "PSA 피어링으로 OCI 대역 경로를 내보내 Cloud SQL이 응답할 수 있게 한다"
  type        = bool
  default     = true
}

variable "psa_peering_name" {
  type    = string
  default = "servicenetworking-googleapis-com"
}

resource "google_compute_ha_vpn_gateway" "this" {
  project = var.project
  region  = var.region
  name    = var.name
  network = var.network
}

resource "google_compute_router" "this" {
  project = var.project
  region  = var.region
  name    = var.name
  network = var.network

  bgp {
    asn               = var.router_asn
    advertise_mode    = "CUSTOM"
    advertised_groups = ["ALL_SUBNETS"]

    dynamic "advertised_ip_ranges" {
      for_each = var.advertised_ip_ranges
      content {
        range = advertised_ip_ranges.value
      }
    }
  }
}

resource "google_compute_external_vpn_gateway" "oci" {
  count = length(var.oci_tunnels) > 0 ? 1 : 0

  project         = var.project
  name            = "${var.name}-oci"
  redundancy_type = "TWO_IPS_REDUNDANCY"

  dynamic "interface" {
    for_each = var.oci_tunnels
    content {
      id         = interface.key
      ip_address = interface.value.oci_ip
    }
  }
}

resource "google_compute_vpn_tunnel" "oci" {
  count = length(var.oci_tunnels)

  project                         = var.project
  region                          = var.region
  name                            = "${var.name}-oci-${count.index}"
  vpn_gateway                     = google_compute_ha_vpn_gateway.this.id
  vpn_gateway_interface           = count.index
  peer_external_gateway           = google_compute_external_vpn_gateway.oci[0].id
  peer_external_gateway_interface = count.index
  router                          = google_compute_router.this.id
  ike_version                     = 2
  shared_secret                   = var.shared_secrets[count.index]
}

resource "google_compute_router_interface" "oci" {
  count = length(var.oci_tunnels)

  project    = var.project
  region     = var.region
  name       = "${var.name}-oci-${count.index}"
  router     = google_compute_router.this.name
  ip_range   = var.oci_tunnels[count.index].gcp_bgp_ip
  vpn_tunnel = google_compute_vpn_tunnel.oci[count.index].name
}

resource "google_compute_router_peer" "oci" {
  count = length(var.oci_tunnels)

  project         = var.project
  region          = var.region
  name            = "${var.name}-oci-${count.index}"
  router          = google_compute_router.this.name
  interface       = google_compute_router_interface.oci[count.index].name
  peer_ip_address = var.oci_tunnels[count.index].oci_bgp_ip
  peer_asn        = var.oci_asn
}

resource "google_compute_network_peering_routes_config" "psa" {
  count = var.export_psa_routes ? 1 : 0

  project              = var.project
  peering              = var.psa_peering_name
  network              = var.network
  import_custom_routes = false
  export_custom_routes = true
}

output "ha_vpn_interface_ips" {
  description = "OCI 쪽 cpe_ip로 넣는 GCP 공인 IP 2개 (인터페이스 0, 1)"
  value       = [for i in google_compute_ha_vpn_gateway.this.vpn_interfaces : i.ip_address]
}

output "router_asn" {
  value = var.router_asn
}

output "tunnel_names" {
  value = google_compute_vpn_tunnel.oci[*].name
}
