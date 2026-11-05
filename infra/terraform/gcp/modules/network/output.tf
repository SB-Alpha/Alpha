output "network_id" {
  value       = google_compute_network.this.id
  description = "생성된 네트워크 아이디"
}

output "subnetwork_id" {
  value       = google_compute_subnetwork.this.id
  description = "생성된 서브넷 아이디"
}

output "vpc_name" {
  value       = google_compute_network.this.name
  description = "VPC 이름"
}

output "subnet_cidrs" {
  value       = [google_compute_subnetwork.this.ip_cidr_range]
  description = "서브넷 CIDR 목록"
}

output "psa_range_cidr" {
  value       = "${google_compute_global_address.this.address}/${google_compute_global_address.this.prefix_length}"
  description = "Cloud SQL 사설 IP 가 있는 PSA 대역"
}
