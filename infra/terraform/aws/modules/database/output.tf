# RDS 엔드포인트 호스트 이름 (IP 아님). 루트의 db_private_ip, db_fqdn 으로 그대로 나간다. 이유는 루트 outputs.tf 의 주석 참고.
output "endpoint_host" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "db_name" {
  value = coalesce(var.db_name, "postgres")
}

output "db_user" {
  value = aws_db_instance.this.username
}

output "db_password" {
  value     = random_password.db.result
  sensitive = true
}

output "instance_name" {
  value = aws_db_instance.this.identifier
}

output "version" {
  value = var.db_version
}

# 파라미터 그룹에서 rds.force_ssl=1 로 고정했으므로 암호화 필수다. 백엔드가 기대하는 문자열(GCP 의 ssl_mode 값)로 돌려준다.
output "ssl_mode" {
  value = "ENCRYPTED_ONLY"
}

output "admin_user" {
  value = aws_db_instance.this.username
}

output "admin_password" {
  value     = random_password.db.result
  sensitive = true
}
