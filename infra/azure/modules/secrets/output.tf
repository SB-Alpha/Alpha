output "key_vault_id" {
  value = azurerm_key_vault.this.id
}

output "db_password_secret_name" {
  value = azurerm_key_vault_secret.db_password.name
}

output "db_password_secret_id" {
  description = "버전 없는 ID. 앱이 항상 최신 값을 읽는다"
  value       = azurerm_key_vault_secret.db_password.versionless_id
}

output "secret_ref_ids" {
  description = "환경 변수 이름 => 버전 없는 비밀 ID"
  value       = { for env, name in var.secret_refs : env => azurerm_key_vault_secret.refs[name].versionless_id }
}
