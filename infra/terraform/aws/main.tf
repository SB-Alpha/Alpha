module "registry" {
  source = "./modules/registry"
  name   = var.service_name
}

module "network" {
  source              = "./modules/network"
  name                = var.service_name
  container_port      = var.container_port
  db_extra_cidrs      = var.cdc_allowed_cidrs
  alb_cloudfront_only = var.enable_https_front
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

# CloudFront 와 ALB 가 공유하는 비밀 값. ALB 는 CloudFront 접근용 IP 대역에서 오는 요청 중에서도 이 헤더가 맞는 것만 앱으로 전달한다.
# (그 IP 대역은 모든 AWS 사용자의 CloudFront 가 쓰므로, 대역만으로는 우리 CloudFront 만 통과시킬 수 없다.)
# 값은 Terraform 상태, CloudFront 배포 설정, ALB 리스너 규칙에 남는다. 계정 안에서 해당 설정을 읽을 수 있는 사람에게는 보인다.
resource "random_password" "origin_verify" {
  count   = var.enable_https_front ? 1 : 0
  length  = 40
  special = false
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
  origin_guard          = var.enable_https_front
  origin_verify_secret  = try(random_password.origin_verify[0].result, null)

  # 실행 역할 권한, 비밀 값, DB, 네트워크가 모두 준비된 뒤에 만들어야 한다
  depends_on = [module.iam, module.secrets, module.database, module.network]
}

# ALB 는 도메인이 없어 https 를 직접 붙일 수 없으므로 앞에 CloudFront 를 둔다. ALB 가 있는 앱 배포 단계(deploy_app)에서만 만든다.
module "https_front" {
  count         = var.deploy_app && var.enable_https_front ? 1 : 0
  source        = "./modules/https_front"
  name          = var.service_name
  origin_domain = module.app[0].alb_dns_name

  origin_verify_secret = random_password.origin_verify[0].result
}
