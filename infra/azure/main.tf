resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.region
}

# 레지스트리, DB, Key Vault 이름은 Azure 전체에서 유일해야 해서 접미사를 붙인다
resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

locals {
  suffix = random_string.suffix.result
}

module "registry" {
  source              = "./modules/registry"
  name                = "${replace(var.service_name, "-", "")}${local.suffix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.region
  sku                 = "Standard"
}

module "network" {
  source              = "./modules/network"
  name                = var.service_name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.region
  vnet_cidr           = var.vnet_cidr
  app_subnet_cidr     = var.app_subnet_cidr
  pe_subnet_cidr      = var.pe_subnet_cidr
  gateway_subnet_cidr = var.gateway_subnet_cidr
  db_peer_cidrs       = var.db_peer_cidrs
}

module "database" {
  source                = "./modules/database"
  name                  = "${var.service_name}-db-${local.suffix}"
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.region
  vnet_id               = module.network.vnet_id
  pe_subnet_id          = module.network.pe_subnet_id
  db_version            = var.db_version
  db_name               = var.db_name
  db_tier               = var.db_tier
  db_disk_size_gb       = var.db_disk_size_gb
  zone                  = var.db_zone
  high_availability     = var.db_high_availability
  standby_zone          = var.db_standby_zone
  backup_retention_days = var.db_backup_retention_days

  max_connections            = var.db_max_connections
  maintenance_day_of_week    = var.db_maintenance_day_of_week
  maintenance_start_hour_utc = var.db_maintenance_start_hour_utc
}

module "secrets" {
  source              = "./modules/secrets"
  name                = "${substr(replace(var.service_name, "-", ""), 0, 15)}-${local.suffix}"
  secret_prefix       = var.service_name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.region
  db_password         = module.database.admin_password
  secret_refs         = var.secret_refs
}

module "iam" {
  source              = "./modules/iam"
  name                = var.service_name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.region
  registry_id         = module.registry.registry_id
  key_vault_id        = module.secrets.key_vault_id
}

module "environment" {
  source              = "./modules/environment"
  name                = var.service_name
  resource_group_name = azurerm_resource_group.this.name
  location            = var.region
  subnet_id           = module.network.app_subnet_id
  zone_redundant      = true
}

module "app" {
  count                     = var.deploy_app ? 1 : 0
  source                    = "./modules/app"
  name                      = var.service_name
  resource_group_name       = azurerm_resource_group.this.name
  environment_id            = module.environment.environment_id
  container_image           = coalesce(var.container_image, "${module.registry.registry_url}/${var.service_name}:latest")
  registry_server           = module.registry.registry_url
  container_port            = var.container_port
  cpu                       = var.cpu
  memory                    = var.memory
  health_check_path         = var.health_check_path
  min_replicas              = var.app_min_replicas
  scale_concurrent_requests = var.app_scale_concurrent_requests
  max_replicas              = var.app_max_replicas
  env_vars                  = var.env_vars
  secret_env                = module.secrets.secret_ref_ids
  identity_id               = module.iam.identity_id
  db_host                   = module.database.fqdn
  db_port                   = module.database.port
  db_name                   = module.database.db_name
  db_user                   = module.database.admin_user
  db_password_secret_id     = module.secrets.db_password_secret_id

  # 권한, 비밀 값, DB, 사설 연결이 모두 준비된 뒤에 만들어야 한다
  depends_on = [module.iam, module.secrets, module.database, module.network]
}

module "monitoring" {
  source              = "./modules/monitoring"
  name                = var.service_name
  resource_group_name = azurerm_resource_group.this.name
  subscription_id     = var.subscription_id
  workspace_id        = module.environment.workspace_id
  db_server_id        = module.database.server_id
  key_vault_id        = module.secrets.key_vault_id
  registry_id         = module.registry.registry_id
  db_burstable        = startswith(var.db_tier, "B_")
  health_locations    = var.health_locations

  db_connection_alert_threshold = var.db_connection_alert_threshold
  monthly_budget_usd            = var.monthly_budget_usd
}

module "dr" {
  count               = var.dr_enabled ? 1 : 0
  source              = "./modules/dr"
  name                = var.service_name
  suffix              = local.suffix
  resource_group_name = "${var.resource_group_name}-dr"
  location            = var.dr_region
  vnet_cidr           = var.dr_vnet_cidr
  app_subnet_cidr     = var.dr_app_subnet_cidr
  pe_subnet_cidr      = var.dr_pe_subnet_cidr
  db_peer_cidrs       = var.db_peer_cidrs
  container_port      = var.container_port

  environment_enabled       = var.dr_app_environment_enabled
  scale_concurrent_requests = var.app_scale_concurrent_requests

  failover          = var.dr_failover
  restore_time      = var.dr_restore_time
  source_server_id  = module.database.server_id
  db_name           = var.db_name
  db_user           = module.database.admin_user
  db_password       = module.database.admin_password
  deploy_app        = var.deploy_app
  container_image   = var.container_image
  cpu               = var.cpu
  memory            = var.memory
  health_check_path = var.health_check_path
  min_replicas      = var.dr_app_min_replicas
  max_replicas      = var.app_max_replicas
  env_vars          = var.env_vars
}
