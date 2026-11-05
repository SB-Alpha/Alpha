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

# db_migration 이 목적지 프로젝트에 VPN 을 만들 때 쓰는 좁은 권한의 서비스 계정
resource "google_service_account" "vpn" {
  account_id   = "alpha-cdc-vpn"
  project      = var.project_id
  display_name = "CDC VPN 구성용 (db_migration)"
}

resource "google_project_iam_member" "vpn" {
  for_each = toset(["roles/compute.networkAdmin", "roles/cloudsql.viewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.vpn.email}"
}

resource "google_service_account_key" "vpn" {
  count              = var.create_vpn_key ? 1 : 0
  service_account_id = google_service_account.vpn.name
}
