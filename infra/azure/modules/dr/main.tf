# 보조 리전 pilot light. 평소에는 네트워크, 레지스트리, 앱 환경만 두고 DB 는 없다.
# 지역 장애 때 failover = true 로 주 DB 의 geo 백업을 복원한다.
# 주 리전 Key Vault 에 의존하지 않도록 DB 비밀번호는 앱 비밀로 직접 넣는다.

locals {
  db_name_dr = "${var.name}-db-dr-${var.suffix}"
  db_fqdn    = "${local.db_name_dr}.postgres.database.azure.com"
}

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
}

module "network" {
  source              = "../network"
  name                = "${var.name}-dr"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  vnet_cidr           = var.vnet_cidr
  app_subnet_cidr     = var.app_subnet_cidr
  pe_subnet_cidr      = var.pe_subnet_cidr
  gateway_subnet_cidr = null
  db_peer_cidrs       = var.db_peer_cidrs
}

module "registry" {
  source              = "../registry"
  name                = "${replace(var.name, "-", "")}dr${var.suffix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  sku                 = "Basic"
}

module "environment" {
  count               = var.environment_enabled ? 1 : 0
  source              = "../environment"
  name                = "${var.name}-dr"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  subnet_id           = module.network.app_subnet_id
  zone_redundant      = false # Korea South 에는 가용 영역이 없다
}

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.name}-dr-app"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
}

resource "azurerm_role_assignment" "registry" {
  scope                = module.registry.registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_private_dns_zone" "this" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.this.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  name                = "${var.name}-dr-link"
  private_dns_zone_id = azurerm_private_dns_zone.this.id
  virtual_network_id  = module.network.vnet_id
}

resource "azurerm_postgresql_flexible_server" "restored" {
  count               = var.failover ? 1 : 0
  name                = local.db_name_dr
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location

  create_mode                       = "GeoRestore"
  source_server_id                  = var.source_server_id
  point_in_time_restore_time_in_utc = var.restore_time
  public_network_access_enabled     = false

  lifecycle {
    precondition {
      condition     = var.restore_time != null
      error_message = "dr_failover 를 켜려면 dr_restore_time 이 필요합니다."
    }
    ignore_changes = [zone]
  }
}

resource "azurerm_private_endpoint" "restored" {
  count               = var.failover ? 1 : 0
  name                = "${local.db_name_dr}-pe"
  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  subnet_id           = module.network.pe_subnet_id

  private_service_connection {
    name                           = "${local.db_name_dr}-pe"
    private_connection_resource_id = azurerm_postgresql_flexible_server.restored[0].id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "postgres"
    private_dns_zone_ids = [azurerm_private_dns_zone.this.id]
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.this]
}

# 앱은 DB 없이 min 0 으로 대기하다가 장애 때 복제본을 올린다. 이미지는 보조 리전 레지스트리에 미리 넣어 둔다.
resource "azurerm_container_app" "this" {
  count                        = var.deploy_app && var.environment_enabled ? 1 : 0
  name                         = substr("${var.name}-dr", 0, 32)
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = module.environment[0].environment_id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = module.registry.registry_url
    identity = azurerm_user_assigned_identity.app.id
  }

  secret {
    name  = "pgpassword"
    value = var.db_password
  }

  ingress {
    external_enabled = true
    target_port      = var.container_port

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    http_scale_rule {
      name                = "http"
      concurrent_requests = tostring(var.scale_concurrent_requests)
    }

    container {
      name   = "app"
      image  = coalesce(var.container_image, "${module.registry.registry_url}/${var.name}:latest")
      cpu    = var.cpu
      memory = "${var.memory / 1024}Gi"

      dynamic "env" {
        for_each = merge(var.env_vars, {
          APP_ENV    = "production"
          PGHOST     = local.db_fqdn
          PGPORT     = "5432"
          PGDATABASE = var.db_name
          PGUSER     = var.db_user
          PGSSLMODE  = "require"
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name        = "PGPASSWORD"
        secret_name = "pgpassword"
      }

      startup_probe {
        transport               = "HTTP"
        port                    = var.container_port
        path                    = var.health_check_path
        interval_seconds        = 5
        failure_count_threshold = 12
      }

      # DB 장애 때 트래픽만 빼고 재시작은 하지 않도록 생존 확인은 TCP 로 한다
      readiness_probe {
        transport               = "HTTP"
        port                    = var.container_port
        path                    = var.health_check_path
        interval_seconds        = 10
        failure_count_threshold = 3
      }

      liveness_probe {
        transport               = "TCP"
        port                    = var.container_port
        interval_seconds        = 10
        failure_count_threshold = 3
      }
    }
  }

  depends_on = [azurerm_role_assignment.registry]
}
