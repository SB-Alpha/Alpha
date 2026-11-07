variable "region" {
  type    = string
  default = "ap-singapore-1"
}

variable "config_profile" {
  type    = string
  default = "DEFAULT"
}

variable "compartment_id" {
  type = string
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "drg_id" {
  description = "oci/ 루트의 drg_id 출력값"
  type        = string
}

variable "tunnels" {
  description = "상대 구간별 터널. 새 실행은 source-0/1, target-0/1 키를 사용한다"
  type = map(object({
    cpe_ip        = string
    peer_cidrs    = list(string)
    peer_asn      = string
    oracle_bgp_ip = string
    peer_bgp_ip   = string
    # 상대 클라우드가 OCI 기본 PFS 그룹을 받지 못할 때만 지정한다
    phase_two_dh_group = optional(string)
  }))
}

variable "shared_secrets" {
  description = "tunnels와 같은 키"
  type        = map(string)
  sensitive   = true
}
