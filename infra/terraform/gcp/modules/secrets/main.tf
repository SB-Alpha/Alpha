resource "google_secret_manager_secret" "db_password" {
  secret_id = "${var.name}-db-password"
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = var.db_password
}

# secret_refs 의 secret 이름만. 값 이전은 Terraform 밖의 별도 작업
resource "google_secret_manager_secret" "refs" {
  for_each  = toset(values(var.secret_refs))
  secret_id = each.value
  project   = var.project_id

  replication {
    auto {}
  }
}

# Cloudflare Worker 가 TARGET 으로 보낼 때 붙이는 X-Alpha-Worker-Secret 과 같은 값.
# 앱이 이 값을 검사해 Worker 를 거치지 않은 직접 호출을 403 으로 막는다 (이슈 #34).
resource "google_secret_manager_secret" "worker_secret" {
  count     = var.protect_ingress ? 1 : 0
  secret_id = "${var.name}-worker-secret"
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "worker_secret" {
  count       = var.protect_ingress ? 1 : 0
  secret      = google_secret_manager_secret.worker_secret[0].id
  secret_data = var.worker_secret

  lifecycle {
    precondition {
      condition     = var.worker_secret != ""
      error_message = "protect_ingress 가 켜져 있으면 worker_secret(TF_VAR_worker_secret)이 있어야 합니다."
    }
  }
}
