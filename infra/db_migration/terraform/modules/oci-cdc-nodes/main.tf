# CDC 노드 N대 (1 또는 3). 노드마다 다른 장애 도메인에 둔다.
# 노드 사설 IP를 고정해 Kafka KRaft 투표자 목록을 미리 알 수 있게 한다.

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

variable "subnet_id" {
  type = string
}

variable "subnet_cidr" {
  type = string
}

variable "node_count" {
  description = "KRaft 투표자 수는 나중에 바꾸기 어렵다. 처음부터 3대로 두고 용량(ocpus·memory_gbs)만 키운다"
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 3], var.node_count)
    error_message = "node_count는 1 또는 3이다 (KRaft 투표자 과반)."
  }
}

variable "fault_domains" {
  description = "노드 순서대로. null이면 OCI가 고른다 (용량 부족 때 재시도용)"
  type        = list(string)
  default     = ["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"]
}

variable "shape" {
  description = "Flex shape. ocpus·memory_gbs를 바꾸면 노드를 다시 만들지 않고 재부팅만 한다"
  type        = string
  default     = "VM.Standard.E5.Flex"
}

variable "ocpus" {
  type    = number
  default = 1
}

variable "memory_gbs" {
  description = "4GB가 최소 (Kafka·Connect heap은 배포 때 메모리에 맞춰 정한다)"
  type        = number
  default     = 4
}

variable "boot_volume_gbs" {
  description = "Kafka 데이터도 부트 볼륨에 둔다"
  type        = number
  default     = 60
}

variable "image_id" {
  description = "비우면 shape에 맞는 Ubuntu 22.04 최신 이미지를 쓴다"
  type        = string
  default     = null
}

variable "ssh_public_key" {
  type = string
}

data "oci_identity_availability_domains" "this" {
  compartment_id = var.compartment_id
}

data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_id
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = var.shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

locals {
  availability_domain = data.oci_identity_availability_domains.this.availability_domains[0].name
  image_id            = coalesce(var.image_id, data.oci_core_images.ubuntu.images[0].id)

  node_ips = [for i in range(var.node_count) : cidrhost(var.subnet_cidr, 10 + i)]
  # KRaft 투표자: "1@10.50.1.10:9093,2@10.50.1.11:9093,..."
  quorum_voters = join(",", [for i, ip in local.node_ips : "${i + 1}@${ip}:9093"])
}

resource "oci_core_instance" "node" {
  count = var.node_count

  compartment_id      = var.compartment_id
  availability_domain = local.availability_domain
  fault_domain        = var.fault_domains[count.index]
  display_name        = "${var.name}-${count.index + 1}"
  shape               = var.shape

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gbs
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_id
    boot_volume_size_in_gbs = var.boot_volume_gbs
  }

  create_vnic_details {
    subnet_id        = var.subnet_id
    private_ip       = local.node_ips[count.index]
    assign_public_ip = false
    hostname_label   = "cdc${count.index + 1}"
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", {
      node_id       = count.index + 1
      node_ip       = local.node_ips[count.index]
      subnet_cidr   = var.subnet_cidr
      node_count    = var.node_count
      quorum_voters = local.quorum_voters
      bootstrap     = join(",", [for ip in local.node_ips : "${ip}:9092"])
    }))
  }

  lifecycle {
    # 이미지가 새로 나와도 노드를 다시 만들지 않는다.
    ignore_changes = [source_details[0].source_id]
  }
}

output "node_ids" {
  value = oci_core_instance.node[*].id
}

output "node_private_ips" {
  value = local.node_ips
}

output "kafka_bootstrap" {
  value = join(",", [for ip in local.node_ips : "${ip}:9092"])
}
