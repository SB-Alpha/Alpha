resource "google_service_account" "app" {
  account_id   = "${substr(var.name, 0, 26)}-run"
  project      = var.project_id
  display_name = "${var.name} Cloud Run 실행 계정"
}

resource "google_secret_manager_secret_iam_member" "app" {
  for_each  = toset(var.secret_ids)
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}