module "registry" {
  source = "./modules/registry"
  name   = var.service_name
}

module "network" {
  source         = "./modules/network"
  name           = var.service_name
  container_port = var.container_port
  db_extra_cidrs = var.cdc_allowed_cidrs
}

module "database" {
  source             = "./modules/database"
  name               = var.service_name
  db_version         = var.db_version
  db_name            = var.db_name
  db_user            = var.db_user
  instance_class     = var.db_instance_class
  disk_size_gb       = var.db_disk_size_gb
  logical_decoding   = var.db_logical_decoding
  private_subnet_ids = module.network.private_subnet_ids
  security_group_ids = [module.network.db_security_group_id]
}

module "secrets" {
  source      = "./modules/secrets"
  name        = var.service_name
  db_password = module.database.db_password
  secret_refs = var.secret_refs
}

module "iam" {
  source = "./modules/iam"
  name   = var.service_name
  secret_arns = concat(
    [module.secrets.db_password_secret_arn],
    values(module.secrets.secret_ref_arns),
  )
}

module "app" {
  count                 = var.deploy_app ? 1 : 0
  source                = "./modules/app"
  name                  = var.service_name
  region                = var.region
  container_image       = coalesce(var.container_image, "${module.registry.registry_url}/${var.service_name}:latest")
  container_port        = var.container_port
  cpu                   = var.cpu
  memory                = var.memory
  health_check_path     = var.health_check_path
  env_vars              = var.env_vars
  secret_env            = module.secrets.secret_ref_arns
  vpc_id                = module.network.vpc_id
  public_subnet_ids     = module.network.public_subnet_ids
  alb_security_group_id = module.network.alb_security_group_id
  app_security_group_id = module.network.app_security_group_id
  execution_role_arn    = module.iam.execution_role_arn
  task_role_arn         = module.iam.task_role_arn
  db_host               = module.database.endpoint_host
  db_port               = module.database.port
  db_name               = module.database.db_name
  db_user               = module.database.db_user
  db_password_arn       = module.secrets.db_password_secret_arn

  # 실행 역할 권한, 비밀 값, DB, 네트워크가 모두 준비된 뒤에 만들어야 한다
  depends_on = [module.iam, module.secrets, module.database, module.network]
}
