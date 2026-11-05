resource "random_password" "db" {
  length  = 24
  special = false # 접속 문자열에 그대로 넣어도 안전하게 영문·숫자만 사용
}

resource "google_sql_database_instance" "this" {
  name                = "${var.name}-db"
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_${var.db_version}"
  deletion_protection = false # 임시 데모 환경이라 destroy 가능하게 둠

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    disk_size         = var.db_disk_size_gb
    availability_type = "ZONAL" # 원본 RDS 가 Single-AZ

    ip_configuration {
      ipv4_enabled    = false # 공개 IP 없이 사설 IP 만 사용
      private_network = var.network_id
    }

    # CDC(논리 복제) 대상이므로 켠다
    dynamic "database_flags" {
      for_each = var.db_logical_decoding ? [1] : []
      content {
        name  = "cloudsql.logical_decoding"
        value = "on"
      }
    }
  }
}

# 기본 DB(postgres)는 인스턴스에 이미 있으므로 다른 이름일 때만 만든다
resource "google_sql_database" "this" {
  count    = var.db_name == "postgres" ? 0 : 1
  name     = var.db_name
  project  = var.project_id
  instance = google_sql_database_instance.this.name
}

resource "google_sql_user" "this" {
  name     = var.db_user
  project  = var.project_id
  instance = google_sql_database_instance.this.name
  password = random_password.db.result
}
