provider "azurerm" {
  subscription_id = var.subscription_id

  # 새 구독에서도 그대로 돌도록 필요한 공급자를 등록한다
  resource_provider_registrations = "core"
  resource_providers_to_register = [
    "Microsoft.App",
    "Microsoft.Consumption",
    "Microsoft.ContainerRegistry",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.Insights",
    "Microsoft.KeyVault",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
  ]

  features {
    key_vault {
      # 임시 데모 환경이라 destroy 후 같은 이름을 다시 쓸 수 있게 둠
      purge_soft_delete_on_destroy = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}
