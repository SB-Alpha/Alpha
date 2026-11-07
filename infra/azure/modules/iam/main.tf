resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.name}-app"
  resource_group_name = var.resource_group_name
  location            = var.location
}

resource "azurerm_role_assignment" "registry" {
  scope                = var.registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "secrets" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}
