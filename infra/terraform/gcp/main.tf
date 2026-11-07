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

  # Cloud SQL 은 network 의 사설 서비스 연결이 끝난 뒤
  depends_on = [module.network]
}

module "secrets" {
  source      = "./modules/secrets"
  project_id  = var.project_id
  name        = var.service_name
  db_password = module.database.db_password
  secret_refs = var.secret_refs

  protect_ingress = var.protect_ingress
  worker_secret   = var.worker_secret
}

module "iam" {
  source         = "./modules/iam"
  project_id     = var.project_id
  name           = var.service_name
  create_vpn_key = var.create_vpn_key
  secret_ids = concat(
    [module.secrets.db_password_secret_id],
    values(module.secrets.secret_ref_ids),
    module.secrets.worker_secret_ids,
  )
}

module "app" {
  count                 = var.deploy_app ? 1 : 0
  source                = "./modules/app"
  project_id            = var.project_id
  region                = var.region
  name                  = var.service_name
  container_image       = coalesce(var.container_image, "${module.registry.registry_url}/${var.service_name}:latest")
  container_port        = var.container_port
  cpu                   = var.cpu
  memory                = var.memory
  health_check_path     = var.health_check_path
  env_vars              = var.env_vars
  secret_env            = module.secrets.secret_ref_ids
  service_account_email = module.iam.service_account_email
  network_id            = module.network.network_id
  subnetwork_id         = module.network.subnetwork_id
  db_host               = module.database.private_ip
  db_port               = module.database.port
  db_name               = module.database.db_name
  db_user               = module.database.db_user
  db_password_secret_id = module.secrets.db_password_secret_id
  worker_secret_id      = one(module.secrets.worker_secret_ids)

  # 서비스 계정 권한, 비밀 값, DB, 사설 연결이 모두 준비된 뒤에 만들어야 한다
  depends_on = [module.iam, module.secrets, module.database, module.network]
}