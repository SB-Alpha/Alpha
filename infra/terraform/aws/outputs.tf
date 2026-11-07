output "app_url" {
  description = "ALB 주소 (트래픽 전환에서 사용). 앱을 배포하기 전(deploy_app=false)에는 null"
  value       = try(module.app[0].app_url, null)
}

# 주의: 이름은 db_private_ip 이지만 AWS 에서는 IP 가 아니라 RDS 엔드포인트 호스트 이름이 들어간다.
# - 이름을 GCP·Azure 와 같게 둔 이유: 백엔드가 이 이름의 출력을 필수로 읽고(REQUIRED_OUTPUTS), 계약(target.db.private_ip)을 바꾸지 않으려는 것.
# - RDS 는 고정 IP 를 주지 않는다(장애 조치 등으로 바뀔 수 있어 IP 를 박아 두면 안 된다). 호스트 이름은 VPC 안에서 사설 IP 로 풀린다.
# - 백엔드는 이 값을 DB 접속 주소(host)로만 쓰고 IP 형식을 검사하지 않는다. 다만 "cloud_sql_private_ip" 지표(SECURE/PUBLIC)는 IP 일 때만 보고된다.
output "db_private_ip" {
  description = "RDS 엔드포인트. RDS 는 고정 IP 가 없어 IP 대신 사설 호스트 이름이 들어간다 (VPC 안에서는 사설 IP 로 풀린다)"
  value       = module.database.endpoint_host
}

# db_private_ip 와 같은 값이다. 백엔드는 읽지 않는 참고용 출력이고, Azure 구성의 db_fqdn 과 이름만 맞췄다.
output "db_fqdn" {
  description = "RDS 엔드포인트 호스트 이름"
  value       = module.database.endpoint_host
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
  description = "DB 비밀번호가 담긴 Secrets Manager 비밀 이름 (값이 아니라 참조)"
  value       = module.secrets.db_password_secret_id
}

output "registry_url" {
  description = "컨테이너 이미지 푸시 대상 주소 (ECR 호스트). 이미지는 <registry_url>/<service_name> 에 올린다"
  value       = module.registry.registry_url
}

output "execution_role_arn" {
  description = "ECS 작업 실행 역할"
  value       = module.iam.execution_role_arn
}

# db_migration 명세용 추가 출력
output "vpc_name" {
  description = "VPC 이름"
  value       = module.network.vpc_name
}

output "subnet_cidrs" {
  description = "앱 서브넷 CIDR 목록"
  value       = module.network.subnet_cidrs
}

output "psa_range_cidr" {
  description = "DB 사설 주소가 있는 대역 (DB 서브넷을 모두 포함하는 /20)"
  value       = module.network.db_range_cidr
}

output "db_instance" {
  description = "RDS 인스턴스 이름"
  value       = module.database.instance_name
}

output "db_version" {
  description = "PostgreSQL 메이저 버전"
  value       = module.database.version
}

output "db_ssl_mode" {
  description = "SSL 모드. 백엔드는 ENCRYPTED_ONLY 를 기대한다 (rds.force_ssl=1 일 때)"
  value       = module.database.ssl_mode
}

# 아래 둘은 secret 값. 결과에 넣지 않고 put_secret 으로 저장한 뒤 이름표(target-admin, target-cloud)만 넘김.
output "db_admin_user" {
  description = "관리자 계정 이름 (target-admin 의 user). RDS 는 마스터 사용자가 앱과 관리자를 겸한다"
  value       = module.database.admin_user
}

output "db_admin_password" {
  description = "마스터 비밀번호 (target-admin 의 password)"
  value       = module.database.admin_password
  sensitive   = true
}

# AWS 목적지는 db_migration 이 VPN 을 만들어 주는 범위가 아직 아니다. 값이 없으면 백엔드는 target-cloud 를 저장하지 않고 경고만 한다.
output "vpn_credentials_json" {
  description = "VPN 구성용 자격 증명. AWS 목적지는 아직 없음(null)"
  value       = null
  sensitive   = true
}
