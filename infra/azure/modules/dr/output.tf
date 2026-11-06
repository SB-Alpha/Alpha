output "registry_url" {
  value = module.registry.registry_url
}

output "app_url" {
  value = try("https://${azurerm_container_app.this[0].ingress[0].fqdn}", null)
}

output "db_fqdn" {
  value = try(azurerm_postgresql_flexible_server.restored[0].fqdn, null)
}
