data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "this" {
  name                       = "kv-${var.name}"
  resource_group_name        = var.resource_group_name
  location                   = var.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false # 임시 데모 환경이라 destroy 가능하게 둠
  soft_delete_retention_days = 7
}

# Terraform 을 실행하는 계정이 비밀을 쓸 수 있어야 한다
resource "azurerm_role_assignment" "deployer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# 역할 할당이 퍼지는 데 시간이 걸린다
resource "time_sleep" "rbac" {
  create_duration = "60s"
  depends_on      = [azurerm_role_assignment.deployer]
}

resource "azurerm_key_vault_secret" "db_password" {
  name         = "${var.secret_prefix}-db-password"
  value        = var.db_password
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [time_sleep.rbac]
}

# Key Vault 는 값 없는 비밀을 만들 수 없어 자리표시자를 넣는다. 값 이전은 Terraform 밖의 별도 작업
resource "azurerm_key_vault_secret" "refs" {
  for_each     = toset(values(var.secret_refs))
  name         = each.value
  value        = "not-set"
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [time_sleep.rbac]

  lifecycle {
    ignore_changes = [value]
  }
}
