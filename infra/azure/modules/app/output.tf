output "app_url" {
  value = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}
