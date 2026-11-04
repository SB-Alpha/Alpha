module "registry" {
  source        = "./modules/registry"
  project_id    = var.project_id
  location      = var.region
  repository_id = "target-app"
}

module "network" {
  source     = "./modules/network"
  name       = var.service_name
  project_id = var.project_id
  region     = var.region
}

module "database" {
  source              = "./modules/database"
  project_id          = var.project_id
  region              = var.region
  name                = var.service_name
  network_id          = module.network.network_id
  db_version          = var.db_version
  db_name             = var.db_name
  db_user             = var.db_user
  db_tier             = var.db_tier
  db_disk_size_gb     = var.db_disk_size_gb
  db_logical_decoding = var.db_logical_decoding

  # Cloud SQL 은 network 의 사설 서비스 연결이 끝난 뒤에 만들어야 한다
  depends_on = [module.network]
}