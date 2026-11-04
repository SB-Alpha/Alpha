output "app_url" {
  description = "Cloud Run 서비스 주소 (트래픽 전환에서 사용)"
  value       = google_cloud_run_v2_service.this.uri
}
