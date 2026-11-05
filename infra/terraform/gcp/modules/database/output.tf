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
