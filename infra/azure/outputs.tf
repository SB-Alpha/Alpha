output "app_url" {
  description = "앱 주소 (트래픽 전환에서 사용). 앱을 배포하기 전(deploy_app=false)에는 null"
  value       = try(module.app[0].app_url, null)
}

output "db_private_ip" {
  description = "Private Endpoint 사설 IP. CDC 가 VPN 으로 접속하는 주소"
  value       = module.database.private_ip
}

output "db_fqdn" {
  description = "DB 주소. VNet 안의 앱이 사설 DNS 로 접속한다"
  value       = module.database.fqdn
}

output "db_port" {
  description = "PostgreSQL 포트"
  value       = module.database.port
}

output "db_name" {
  description = "앱이 쓰는 DB 이름"
  value       = module.database.db_name
}

output "db_user" {
  description = "앱이 쓰는 DB 사용자 이름. Azure 는 일반 역할을 Terraform 으로 만들 수 없어 관리자 계정을 쓴다"
  value       = module.database.admin_user
}

output "db_password_secret_id" {
  description = "DB 비밀번호가 담긴 Key Vault 비밀 이름 (값이 아니라 참조)"
  value       = module.secrets.db_password_secret_name
}

output "registry_url" {
  description = "컨테이너 이미지 푸시 대상 주소"
  value       = module.registry.registry_url
}

output "identity_client_id" {
  description = "앱이 사용하는 관리 ID 의 클라이언트 ID"
  value       = module.iam.client_id
}

# db_migration 명세용 추가 출력. 이름은 GCP 출력과 맞춘다.
output "vpc_name" {
  description = "VNet 이름"
  value       = module.network.vnet_name
}

output "resource_group_name" {
  description = "주 리전 리소스 그룹"
  value       = azurerm_resource_group.this.name
}

output "subnet_cidrs" {
  description = "앱 서브넷 CIDR 목록"
  value       = module.network.subnet_cidrs
}

output "psa_range_cidr" {
  description = "DB 사설 IP 가 있는 대역. Azure 에서는 Private Endpoint 서브넷"
  value       = module.network.pe_subnet_cidr
}

output "db_instance" {
  description = "PostgreSQL 서버 이름"
  value       = module.database.instance_name
}

output "db_version" {
  description = "PostgreSQL 메이저 버전"
  value       = module.database.version
}

output "db_ssl_mode" {
  description = "SSL 모드. require_secure_transport 가 켜져 있으면 ENCRYPTED_ONLY"
  value       = module.database.ssl_mode
}

output "gateway_subnet_id" {
  description = "VPN Gateway 가 들어갈 서브넷"
  value       = module.network.gateway_subnet_id
}

output "dr_registry_url" {
  description = "보조 리전 레지스트리 주소. 장애 전환 전에 같은 digest 이미지를 넣어 둔다"
  value       = try(module.dr[0].registry_url, null)
}

output "dr_app_url" {
  description = "보조 리전 앱 주소. 장애 때 Worker 를 이 주소로 돌린다"
  value       = try(module.dr[0].app_url, null)
}

output "dr_db_fqdn" {
  description = "geo-restore 한 보조 리전 DB 주소. dr_failover 전에는 null"
  value       = try(module.dr[0].db_fqdn, null)
}

# 아래는 secret 값. 결과에 넣지 않고 put_secret 으로 저장한 뒤 이름표(target-admin)만 넘김.
output "db_admin_user" {
  description = "관리자 계정 이름 (target-admin 의 user)"
  value       = module.database.admin_user
}

output "db_admin_password" {
  description = "관리자 비밀번호 (target-admin 의 password)"
  value       = module.database.admin_password
  sensitive   = true
}
