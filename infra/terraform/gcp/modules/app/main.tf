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
}

resource "google_cloud_run_v2_service" "this" {
  name                = var.name
  project             = var.project_id
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false # 임시 데모 환경이라 destroy 가능하게 둠

  template {
    service_account = var.service_account_email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    # 사설 IP 의 Cloud SQL 에 닿도록 VPC 를 연결
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = var.network_id
        subnetwork = var.subnetwork_id
      }
    }

    containers {
      image = var.container_image

      ports {
        container_port = var.container_port
      }

      resources {
        limits = {
          cpu    = tostring(var.cpu)
          memory = "${var.memory}Mi"
        }
      }

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
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = var.health_check_path
        }
        period_seconds    = 5
        failure_threshold = 12
      }
    }
  }
}

# 사용자 요청(Cloudflare 경유)을 받아야 하므로 누구나 호출 가능하게 연다
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = google_cloud_run_v2_service.this.project
  location = google_cloud_run_v2_service.this.location
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
