resource "aws_secretsmanager_secret" "db_password" {
  name                    = "${var.name}-db-password"
  recovery_window_in_days = 0 # 임시 데모 환경이라 destroy 하면 바로 삭제
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = var.db_password
}

# secret_refs 의 secret 이름만. 값 이전은 Terraform 밖의 별도 작업
resource "aws_secretsmanager_secret" "refs" {
  for_each                = toset(values(var.secret_refs))
  name                    = each.value
  recovery_window_in_days = 0
}
