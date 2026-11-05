# CDC 노드가 놓이는 사설 네트워크.
# 인터넷으로는 NAT로만 나가고, AWS·GCP 대역은 DRG(VPN)로 보낸다.

terraform {
  required_providers {
    oci = {
      source = "oracle/oci"
    }
  }
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
  description = "VPN 너머 대역. DRG로 보낸다"
  type        = list(string)
}

resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_id
  display_name   = var.name
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = "cdc"
}

resource "oci_core_nat_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name}-nat"
}

resource "oci_core_drg" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.name}-drg"
}

resource "oci_core_drg_attachment" "vcn" {
  drg_id       = oci_core_drg.this.id
  display_name = "${var.name}-vcn"

  network_details {
    id   = oci_core_vcn.this.id
    type = "VCN"
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name}-private"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.this.id
  }

  dynamic "route_rules" {
    for_each = var.peer_cidrs
    content {
      destination       = route_rules.value
      destination_type  = "CIDR_BLOCK"
      network_entity_id = oci_core_drg.this.id
    }
  }

  depends_on = [oci_core_drg_attachment.vcn]
}

# 노드끼리는 모두 허용(Kafka·Connect 클러스터 통신), 밖에서 들어오는 것은 없다.
# 제어 API는 cloudflared가 밖으로 맺은 연결로만 들어온다.
resource "oci_core_security_list" "private" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name}-private"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  ingress_security_rules {
    source   = var.subnet_cidr
    protocol = "all"
  }
}

resource "oci_core_subnet" "private" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = var.subnet_cidr
  display_name               = "${var.name}-private"
  dns_label                  = "nodes"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.private.id]
}

output "vcn_id" {
  value = oci_core_vcn.this.id
}

output "subnet_id" {
  value = oci_core_subnet.private.id
}

output "subnet_cidr" {
  value = oci_core_subnet.private.cidr_block
}

output "drg_id" {
  value = oci_core_drg.this.id
}
