# VPN 루트(oci-vpn, aws-vpn, gcp)의 Terraform 상태를 두는 버킷.
# 터널 공유 비밀이 상태에 들어가므로 비공개로 두고 버전을 남긴다.

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_id
}

resource "oci_objectstorage_bucket" "tfstate" {
  compartment_id = var.compartment_id
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "${var.name}-tfstate"
  access_type    = "NoPublicAccess"
  versioning     = "Enabled"
}
