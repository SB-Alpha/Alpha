output "server_id" {
  value = azurerm_postgresql_flexible_server.this.id
}

output "instance_name" {
  value = azurerm_postgresql_flexible_server.this.name
}

output "fqdn" {
  value = azurerm_postgresql_flexible_server.this.fqdn
}

output "private_ip" {
  description = "Private Endpoint 는 자원을 지우기 전까지 IP 가 바뀌지 않는다"
  value       = azurerm_private_endpoint.this.private_service_connection[0].private_ip_address
}

output "port" {
  value = 5432
}

output "db_name" {
  value = var.db_name
}

output "version" {
  value = azurerm_postgresql_flexible_server.this.version
}

output "ssl_mode" {
  value = azurerm_postgresql_flexible_server_configuration.require_tls.value == "ON" ? "ENCRYPTED_ONLY" : "ALLOW_UNENCRYPTED"
}

output "admin_user" {
  value = azurerm_postgresql_flexible_server.this.administrator_login
}

output "admin_password" {
  value     = random_password.admin.result
  sensitive = true
}
