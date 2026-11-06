output "registry_id" {
  value = azurerm_container_registry.this.id
}

output "registry_url" {
  value = azurerm_container_registry.this.login_server
}
