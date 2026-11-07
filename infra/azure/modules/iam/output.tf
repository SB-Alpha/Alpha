output "identity_id" {
  value = azurerm_user_assigned_identity.app.id
}

output "client_id" {
  value = azurerm_user_assigned_identity.app.client_id
}
