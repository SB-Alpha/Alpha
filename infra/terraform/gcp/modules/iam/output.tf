output "service_account_email" {
  description = "Cloud Run 이 사용할 서비스 계정 이메일"
  value       = google_service_account.app.email
}
