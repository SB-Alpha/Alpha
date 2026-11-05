variable "project" {
  type = string
}

variable "region" {
  type    = string
  default = "asia-northeast1"
}

variable "network" {
  type = string
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "oci_tunnels" {
  type = list(object({
    oci_ip     = string
    gcp_bgp_ip = string
    oci_bgp_ip = string
  }))
  default = []
}

variable "shared_secrets" {
  type      = list(string)
  default   = []
  sensitive = true
}

variable "advertised_ip_ranges" {
  description = "Cloud SQL PSA 할당 대역"
  type        = list(string)
  default     = []
}

variable "export_psa_routes" {
  type    = bool
  default = true
}
