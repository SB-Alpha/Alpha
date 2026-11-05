output "private_ip" {
  description = "Cloud SQL 사설 IP"
  value       = google_sql_database_instance.this.private_ip_address
}

output "port" {
  description = "PostgreSQL 포트"
  value       = 5432
}

output "db_name" {
  description = "앱이 쓰는 DB 이름"
  value       = var.db_name
}

output "db_user" {
  description = "앱이 쓰는 DB 사용자 이름"
  value       = google_sql_user.this.name
}

output "db_password" {
  description = "앱 DB 비밀번호. secrets 모듈이 Secret Manager 에 넣는다"
  value       = random_password.db.result
  sensitive   = true
}

output "instance_name" {
  description = "Cloud SQL 인스턴스 이름"
  value       = google_sql_database_instance.this.name
}

output "version" {
  description = "Cloud SQL 데이터베이스 버전 (예: POSTGRES_16)"
  value       = google_sql_database_instance.this.database_version
}

output "ssl_mode" {
  description = "Cloud SQL SSL 모드"
  value       = google_sql_database_instance.this.settings[0].ip_configuration[0].ssl_mode
}

output "admin_user" {
  description = "관리자 계정 이름"
  value       = google_sql_user.admin.name
}

output "admin_password" {
  description = "postgres 관리자 비밀번호. 이름표 target-admin 으로 저장해 db_migration 에 넘긴다"
  value       = random_password.admin.result
  sensitive   = true
}
