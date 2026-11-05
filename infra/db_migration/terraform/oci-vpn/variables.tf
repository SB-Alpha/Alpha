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
  description = "키 이름: aws-0, aws-1, gcp-0, gcp-1"
  type = map(object({
    cpe_ip        = string
    peer_cidrs    = list(string)
    peer_asn      = string
    oracle_bgp_ip = string
    peer_bgp_ip   = string
  }))
}

variable "shared_secrets" {
  description = "tunnels와 같은 키"
  type        = map(string)
  sensitive   = true
}
