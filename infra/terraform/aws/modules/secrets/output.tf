output "db_password_secret_id" {
  value = aws_secretsmanager_secret.db_password.name
}

output "db_password_secret_arn" {
  value = aws_secretsmanager_secret.db_password.arn
}

output "secret_ref_ids" {
  value = { for env, name in var.secret_refs : env => aws_secretsmanager_secret.refs[name].name }
}

# ECS 는 이름이 아니라 ARN 으로 비밀을 읽는다
output "secret_ref_arns" {
  value = { for env, name in var.secret_refs : env => aws_secretsmanager_secret.refs[name].arn }
}
