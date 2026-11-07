resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name}-logs"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "this" {
  name                       = var.name
  resource_group_name        = var.resource_group_name
  location                   = var.location
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  infrastructure_subnet_id   = var.subnet_id
  zone_redundancy_enabled    = var.zone_redundant

  # 관리 ID, 서브넷 /27 을 쓰는 워크로드 프로필 환경. 사용량 과금 프로필만 쓴다
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}
