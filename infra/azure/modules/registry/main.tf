data "azurerm_client_config" "current" {}

resource "azurerm_container_registry" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  admin_enabled       = false # 앱은 관리 ID 로 받는다
}

# Terraform 을 실행한 신분(az login)이 이미지를 올릴 수 있게 한다. 백엔드의 이미지 복사(crane)가 이 신분의 토큰으로 push 한다.
# 구독 Owner 에는 겹치지만 문제없다. 역할 부여가 퍼지는 데 시간이 걸려 부여 직후 push 는 실패할 수 있다 (백엔드가 재시도한다).
resource "azurerm_role_assignment" "push" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_client_config.current.object_id
}
