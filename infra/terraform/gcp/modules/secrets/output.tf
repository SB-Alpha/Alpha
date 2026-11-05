output "db_password_secret_id" {
  description = "DB 비밀번호가 담긴 secret의 이름 (참조)"
  value       = google_secret_manager_secret.db_password.secret_id
}

output "secret_ref_ids" {
  description = "환경 변수 이름 => 만들어진 secret 이름"
  value       = { for env, name in var.secret_refs : env => google_secret_manager_secret.refs[name].secret_id }
}
