output "environment_id" {
  value = azurerm_container_app_environment.this.id
}

output "workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}
