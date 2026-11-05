output "service_account_email" {
  description = "Cloud Run 이 사용할 서비스 계정 이메일"
  value       = google_service_account.app.email
}

output "vpn_service_account_email" {
  description = "VPN 구성용 서비스 계정 이메일"
  value       = google_service_account.vpn.email
}

output "vpn_credentials_json" {
  description = "VPN 구성용 서비스 계정 키(JSON). 이름표 target-cloud 로 저장해 db_migration 에 넘긴다"
  value       = try(base64decode(google_service_account_key.vpn[0].private_key), null)
  sensitive   = true
}
