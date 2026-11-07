# Azure 는 일반 DB 역할을 만드는 자원이 없어 관리자 계정 하나만 만든다.
# postgres, azure_superuser 같은 이름은 예약되어 있어 쓸 수 없다.
locals {
  admin_user = "alphaadmin"
}

resource "random_password" "admin" {
  length      = 24
  special     = false # 접속 문자열에 그대로 넣어도 안전하게 영문·숫자만 사용
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  version             = var.db_version
  sku_name            = var.db_tier
  storage_mb          = var.db_disk_size_gb * 1024
  auto_grow_enabled   = true # 가득 차면 읽기 전용으로 바뀌므로 먼저 늘린다
  zone                = var.zone

  administrator_login    = local.admin_user
  administrator_password = random_password.admin.result

  backup_retention_days        = var.backup_retention_days
  geo_redundant_backup_enabled = true # 서버를 만들 때만 정할 수 있다

  # 접속은 Private Endpoint 로만 받는다
  public_network_access_enabled = false

  maintenance_window {
    day_of_week  = var.maintenance_day_of_week
    start_hour   = var.maintenance_start_hour_utc
    start_minute = 0
  }

  dynamic "high_availability" {
    for_each = var.high_availability ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = var.standby_zone
    }
  }

  lifecycle {
    # 장애 조치가 일어나면 Azure 가 주·대기 영역을 맞바꾼다
    ignore_changes = [zone, high_availability[0].standby_availability_zone]

    precondition {
      condition     = !var.high_availability || !startswith(var.db_tier, "B_")
      error_message = "Burstable 등급은 영역 중복 HA 를 지원하지 않습니다. db_tier 를 General Purpose 로 올리세요."
    }
  }
}

# 서버를 지우면 백업도 함께 사라지므로 실수로 지우지 못하게 잠근다
resource "azurerm_management_lock" "server" {
  name       = "${var.name}-no-delete"
  scope      = azurerm_postgresql_flexible_server.this.id
  lock_level = "CanNotDelete"
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = "ON"
}

# 이 값은 등급을 바꿔도 따라오지 않아서 올릴 때 함께 지정한다. 바꾸면 서버를 재시작해야 적용된다
resource "azurerm_postgresql_flexible_server_configuration" "max_connections" {
  count     = var.max_connections == null ? 0 : 1
  name      = "max_connections"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = tostring(var.max_connections)
}

# 기본 DB(postgres)는 서버에 이미 있으므로 다른 이름일 때만 만든다
resource "azurerm_postgresql_flexible_server_database" "this" {
  count     = var.db_name == "postgres" ? 0 : 1
  name      = var.db_name
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_private_dns_zone" "this" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  name                = "${var.name}-link"
  private_dns_zone_id = azurerm_private_dns_zone.this.id
  virtual_network_id  = var.vnet_id
}

resource "azurerm_private_endpoint" "this" {
  name                = "${var.name}-pe"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.pe_subnet_id

  private_service_connection {
    name                           = "${var.name}-pe"
    private_connection_resource_id = azurerm_postgresql_flexible_server.this.id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "postgres"
    private_dns_zone_ids = [azurerm_private_dns_zone.this.id]
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.this]
}
