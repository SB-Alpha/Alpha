locals {
  # 구독 Owner 내장 역할. 알림 주소를 코드에 적지 않고 역할에 보낸다
  owner_role_id = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"

  db_alerts = merge(
    {
      alive       = { metric = "is_db_alive", aggregation = "Maximum", operator = "LessThan", threshold = 1, severity = 0 }
      storage     = { metric = "storage_percent", aggregation = "Maximum", operator = "GreaterThan", threshold = 80, severity = 1 }
      cpu         = { metric = "cpu_percent", aggregation = "Average", operator = "GreaterThan", threshold = 80, severity = 2 }
      memory      = { metric = "memory_percent", aggregation = "Average", operator = "GreaterThan", threshold = 85, severity = 2 }
      connections = { metric = "active_connections", aggregation = "Average", operator = "GreaterThan", threshold = var.db_connection_alert_threshold, severity = 2 }
    },
    # CPU 크레딧은 Burstable 등급에만 있다
    var.db_burstable ? {
      cpu_credits = { metric = "cpu_credits_remaining", aggregation = "Average", operator = "LessThan", threshold = 30, severity = 1 }
    } : {}
  )

  diagnostics = {
    db       = { id = var.db_server_id, logs = ["PostgreSQLLogs"] }
    keyvault = { id = var.key_vault_id, logs = ["AuditEvent"] }
    registry = { id = var.registry_id, logs = ["ContainerRegistryLoginEvents", "ContainerRegistryRepositoryEvents"] }
  }
}

resource "azurerm_monitor_action_group" "owners" {
  name                = "${var.name}-owners"
  resource_group_name = var.resource_group_name
  short_name          = "owners"

  arm_role_receiver {
    name                    = "owner"
    role_id                 = local.owner_role_id
    use_common_alert_schema = true
  }
}

resource "azurerm_monitor_diagnostic_setting" "this" {
  for_each                   = local.diagnostics
  name                       = "to-log-analytics"
  target_resource_id         = each.value.id
  log_analytics_workspace_id = var.workspace_id

  dynamic "enabled_log" {
    for_each = each.value.logs
    content {
      category = enabled_log.value
    }
  }

  enabled_metric {
    category = "AllMetrics"
  }
}

resource "azurerm_monitor_metric_alert" "db" {
  for_each            = local.db_alerts
  name                = "${var.name}-db-${each.key}"
  resource_group_name = var.resource_group_name
  scopes              = [var.db_server_id]
  severity            = each.value.severity
  frequency           = "PT1M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
    metric_name      = each.value.metric
    aggregation      = each.value.aggregation
    operator         = each.value.operator
    threshold        = each.value.threshold
  }

  action {
    action_group_id = azurerm_monitor_action_group.owners.id
  }
}

# 계획 점검과 리전 장애 공지를 받는다
resource "azurerm_monitor_activity_log_alert" "service_health" {
  name                = "${var.name}-service-health"
  resource_group_name = var.resource_group_name
  location            = "global"
  scopes              = ["/subscriptions/${var.subscription_id}"]

  criteria {
    category = "ServiceHealth"

    service_health {
      locations = var.health_locations
    }
  }

  action {
    action_group_id = azurerm_monitor_action_group.owners.id
  }
}

resource "azurerm_consumption_budget_subscription" "this" {
  count           = var.monthly_budget_usd == null ? 0 : 1
  name            = "${var.name}-monthly"
  subscription_id = "/subscriptions/${var.subscription_id}"
  amount          = var.monthly_budget_usd
  time_grain      = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00'Z'", timestamp())
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      threshold      = notification.value
      operator       = "GreaterThan"
      threshold_type = "Actual"
      contact_roles  = ["Owner"]
    }
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
