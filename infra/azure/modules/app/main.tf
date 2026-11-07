locals {
  plain_env = merge(var.env_vars, {
    APP_ENV    = var.app_env
    PGHOST     = var.db_host
    PGPORT     = tostring(var.db_port)
    PGDATABASE = var.db_name
    PGUSER     = var.db_user
    PGSSLMODE  = "require"
  })

  secret_env = merge(var.secret_env, {
    PGPASSWORD = var.db_password_secret_id
  })

  # Container Apps 비밀 이름은 소문자·숫자·하이픈만 허용한다
  secret_name = { for env in keys(local.secret_env) : env => replace(lower(env), "_", "-") }
}

resource "azurerm_container_app" "this" {
  name                         = substr(var.name, 0, 32)
  resource_group_name          = var.resource_group_name
  container_app_environment_id = var.environment_id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  registry {
    server   = var.registry_server
    identity = var.identity_id
  }

  dynamic "secret" {
    for_each = local.secret_env
    content {
      name                = local.secret_name[secret.key]
      identity            = var.identity_id
      key_vault_secret_id = secret.value
    }
  }

  # 사용자 요청(Cloudflare 경유)을 받아야 하므로 공개 주소를 연다
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
      image  = var.container_image
      cpu    = var.cpu
      memory = "${var.memory / 1024}Gi"

      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name        = env.key
          secret_name = local.secret_name[env.key]
        }
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
}
