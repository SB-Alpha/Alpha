variable "region" {
  type    = string
  default = "ap-northeast-2"
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
  type    = string
  default = "10.50.0.0/16"
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
