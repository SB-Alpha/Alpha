output "node_private_ips" {
  value = module.nodes.node_private_ips
}

output "kafka_bootstrap" {
  value = module.nodes.kafka_bootstrap
}

output "drg_id" {
  description = "oci-vpn 루트의 drg_id"
  value       = module.network.drg_id
}

output "compartment_id" {
  value = var.compartment_id
}

output "tfstate_bucket" {
  value = oci_objectstorage_bucket.tfstate.name
}

output "tfstate_namespace" {
  value = data.oci_objectstorage_namespace.this.namespace
}
