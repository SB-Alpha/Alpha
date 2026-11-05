output "registry_url" {
  description = "이미지 저장소 주소 (이미지 푸시 대상)"
  value       = "${var.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.this.repository_id}"
}
