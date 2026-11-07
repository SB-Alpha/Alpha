variable "region" {
  type = string
}

variable "profile" {
  type    = string
  default = null
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "vpc_id" {
  type = string
}

variable "route_table_ids" {
  type = list(string)
}

variable "db_security_group_id" {
  type    = string
  default = null
}

variable "oci_cidr" {
  description = "RDS에 접속을 허용할 CDC 노드 서브넷 (terraform/oci의 subnet_cidr)"
  type        = string
}

variable "db_port" {
  description = "원본 DB 포트"
  type        = number
}

variable "tunnels" {
  type = list(object({
    oci_ip      = string
    inside_cidr = string
  }))
}

variable "shared_secrets" {
  type      = list(string)
  sensitive = true
}
