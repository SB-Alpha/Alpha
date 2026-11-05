# OCI 쪽 CDC 서비스: 사설 네트워크, CDC 노드. VPN은 oci-vpn/ 루트가 실행마다 만든다.
# 인증은 ~/.oci/config 프로필을 쓴다. 키·OCID는 이 레포에 넣지 않는다.

terraform {
  required_version = ">= 1.6"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 7.0"
    }
  }
}

provider "oci" {
  region              = var.region
  config_file_profile = var.config_profile
}

module "network" {
  source = "../modules/oci-network"

  compartment_id = var.compartment_id
  name           = var.name
  vcn_cidr       = var.vcn_cidr
  subnet_cidr    = var.subnet_cidr
  peer_cidrs     = var.peer_cidrs
}

module "nodes" {
  source = "../modules/oci-cdc-nodes"

  compartment_id = var.compartment_id
  name           = var.name
  subnet_id      = module.network.subnet_id
  subnet_cidr    = module.network.subnet_cidr
  node_count     = var.node_count
  fault_domains  = var.fault_domains
  shape          = var.node_shape
  ocpus          = var.node_ocpus
  memory_gbs     = var.node_memory_gbs
  ssh_public_key = var.ssh_public_key
}
