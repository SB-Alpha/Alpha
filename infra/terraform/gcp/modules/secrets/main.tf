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
