output "app_url" {
  description = "Cloud Run 주소 (트래픽 전환에서 사용). 앱을 배포하기 전(deploy_app=false)에는 null"
  value       = try(module.app[0].app_url, null)
}

output "db_private_ip" {
  description = "Cloud SQL 사설 IP"
  value       = module.database.private_ip
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
  description = "앱이 쓰는 DB 사용자 이름"
  value       = module.database.db_user
}

output "db_password_secret_id" {
  description = "DB 비밀번호가 담긴 Secret Manager 비밀 이름 (값이 아니라 참조)"
  value       = module.secrets.db_password_secret_id
}

output "registry_url" {
  description = "컨테이너 이미지 푸시 대상 주소"
  value       = module.registry.registry_url
}

output "service_account_email" {
  description = "Cloud Run 이 사용하는 서비스 계정 이메일"
  value       = module.iam.service_account_email
}

# db_migration 명세용 추가 출력
output "vpc_name" {
  description = "VPC 이름"
  value       = module.network.vpc_name
}

output "subnet_cidrs" {
  description = "서브넷 CIDR 목록"
  value       = module.network.subnet_cidrs
}

output "psa_range_cidr" {
  description = "Cloud SQL 사설 IP 가 있는 PSA 대역"
  value       = module.network.psa_range_cidr
}

output "db_instance" {
  description = "Cloud SQL 인스턴스 이름"
  value       = module.database.instance_name
}

output "db_version" {
  description = "Cloud SQL 데이터베이스 버전"
  value       = module.database.version
}

output "db_ssl_mode" {
  description = "Cloud SQL SSL 모드"
  value       = module.database.ssl_mode
}

output "vpn_service_account_email" {
  description = "VPN 구성용 서비스 계정 이메일"
  value       = module.iam.vpn_service_account_email
}

# 아래 둘은 secret 값. 결과에 넣지 않고 put_secret 으로 저장한 뒤 이름표(target-admin, target-cloud)만 넘김.
output "db_admin_user" {
  description = "관리자 계정 이름 (target-admin 의 user)"
  value       = module.database.admin_user
}

output "db_admin_password" {
  description = "postgres 관리자 비밀번호 (target-admin 의 password)"
  value       = module.database.admin_password
  sensitive   = true
}

output "vpn_credentials_json" {
  description = "VPN 구성용 서비스 계정 키(JSON) (target-cloud)"
  value       = module.iam.vpn_credentials_json
  sensitive   = true
}
