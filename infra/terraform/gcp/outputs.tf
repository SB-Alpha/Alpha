# TargetEnv: 플랫폼이 `terraform output -json` 으로 읽어 가는 값들
# 비밀번호 값은 내보내지 않고, 비밀의 이름(참조)만 내보냄.

output "app_url" {
  description = "Cloud Run 주소 (트래픽 전환에서 사용)"
  value       = module.app.app_url
}

output "db_private_ip" {
  description = "Cloud SQL 사설 IP"
  value       = module.database.private_ip
}

output "db_port" {
  description = "PostgreSQL 포트"
  value       = module.database.port
}

output "db_name" {
  description = "앱이 쓰는 DB 이름"
  value       = module.database.db_name
}

output "db_user" {
  description = "앱이 쓰는 DB 사용자 이름"
  value       = module.database.db_user
}

output "db_password_secret_id" {
  description = "DB 비밀번호가 담긴 Secret Manager 비밀 이름 (값이 아니라 참조)"
  value       = module.secrets.db_password_secret_id
}

output "registry_url" {
  description = "컨테이너 이미지 푸시 대상 주소"
  value       = module.registry.registry_url
}

output "service_account_email" {
  description = "Cloud Run 이 사용하는 서비스 계정 이메일"
  value       = module.iam.service_account_email
}
