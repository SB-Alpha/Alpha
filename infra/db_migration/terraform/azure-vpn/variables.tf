variable "resource_group" {
  type = string
}

variable "location" {
  type = string
}

variable "vnet_name" {
  type = string
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "sku" {
  type    = string
  default = "VpnGw1AZ"
}

variable "gateway_asn" {
  type    = number
  default = 65515
}

variable "gateway_bgp_ips" {
  type = list(string)
}

variable "oci_tunnels" {
  type = list(object({
    oci_ip     = string
    oci_bgp_ip = string
  }))
  default = []
}

variable "shared_secrets" {
  type      = list(string)
  default   = []
  sensitive = true
}
