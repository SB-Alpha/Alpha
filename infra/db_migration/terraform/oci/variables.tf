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

variable "vcn_cidr" {
  type    = string
  default = "10.50.0.0/16"
}

variable "subnet_cidr" {
  type    = string
  default = "10.50.1.0/24"
}

variable "peer_cidrs" {
  description = "DRG(VPN)로 보낼 대역. 원본·목적지 대역은 실행마다 달라 사설 대역 전체를 둔다"
  type        = list(string)
}

variable "node_count" {
  type    = number
  default = 3
}

# 용량은 이 세 값만 바꿔 apply하고 deploy.sh를 다시 돌리면 된다.
variable "node_shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
}

variable "node_ocpus" {
  type    = number
  default = 1
}

variable "node_memory_gbs" {
  type    = number
  default = 4
}

variable "ssh_public_key" {
  type = string
}

variable "admin_cidrs" {
  description = "Bastion에 접속할 운영자 공인 IP (/32). 비우면 Bastion을 만들지 않는다"
  type        = list(string)
  default     = []
}

variable "ssh_sessions" {
  type    = bool
  default = true
}

variable "fault_domains" {
  type    = list(string)
  default = ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]
}
