output "network_id" {
  value       = google_compute_network.this.id
  description = "생성된 네트워크 아이디"
}

output "subnetwork_id" {
  value       = google_compute_subnetwork.this.id
  description = "생성된 서브넷 아이디"
}