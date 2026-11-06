variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "vnet_cidr" {
  type = string
}

variable "app_subnet_cidr" {
  type = string
}

variable "pe_subnet_cidr" {
  type = string
}

variable "gateway_subnet_cidr" {
  description = "null 이면 만들지 않는다 (보조 리전)"
  type        = string
  default     = null
}

variable "db_peer_cidrs" {
  type = list(string)
}
