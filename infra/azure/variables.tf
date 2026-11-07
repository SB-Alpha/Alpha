variable "subscription_id" {
  description = "Azure 구독 ID"
  type        = string
}

variable "resource_group_name" {
  description = "주 리전 리소스 그룹 이름"
  type        = string
  default     = "rg-alpha-target"
}

variable "region" {
  description = "주 리전"
  type        = string
  default     = "koreacentral"
}

# 아래부터는 MigrationPlan.target_infra 와 이름이 1:1로 대응하는 입력입니다.
# 값이 없는 키는 tfvars 에서 빠져서 오므로, 선택 항목은 기본값을 둡니다.
variable "service_name" {
  description = "서비스 이름 (자원 이름의 기준)"
  type        = string
}

variable "source_image" {
  description = "원본 ECR 이미지 주소. Container Registry 복사는 Terraform 밖에서 처리"
  type        = string
  default     = null
}

variable "deploy_app" {
  description = "앱을 배포할지. db_migration 이 streaming 에 들어가 테이블이 만들어진 뒤에 true 로 바꿔 두 번째 apply 를 한다"
  type        = bool
  default     = false
}

variable "container_image" {
  description = "앱이 실행할 이미지 주소. 비우면 registry 주소 기준 기본값을 씀"
  type        = string
  default     = null
}

variable "container_port" {
  description = "컨테이너 포트"
  type        = number
  default     = 8080
}

variable "cpu" {
  description = "vCPU. memory 와 함께 Container Apps 가 허용하는 조합이어야 한다"
  type        = number
  default     = 0.5
}

variable "memory" {
  description = "메모리 (MiB). vCPU 1개당 2048 MiB"
  type        = number
  default     = 1024

  validation {
    condition     = contains([512, 1024, 1536, 2048, 2560, 3072, 3584, 4096], var.memory)
    error_message = "memory 는 512 부터 4096 까지 512 단위여야 합니다."
  }
}

variable "health_check_path" {
  description = "상태 확인 경로"
  type        = string
  default     = "/health/ready"
}

variable "app_min_replicas" {
  description = "앱 최소 복제본 수. AZ 장애를 견디려면 2 이상"
  type        = number
  default     = 2
}

variable "app_max_replicas" {
  description = "앱 최대 복제본 수"
  type        = number
  default     = 4
}

variable "app_scale_concurrent_requests" {
  description = "복제본 하나가 받을 동시 요청 수. 넘으면 복제본을 늘린다"
  type        = number
  default     = 50
}

variable "env_vars" {
  description = "비밀이 아닌 환경 변수 (이름 => 값). PG*, APP_ENV 는 Terraform 이 채움"
  type        = map(string)
  default     = {}
}

variable "secret_refs" {
  description = "환경 변수 이름 => Key Vault 비밀 이름"
  type        = map(string)
  default     = {}
}

variable "db_version" {
  description = "PostgreSQL 메이저 버전"
  type        = string
  default     = "16"
}

variable "db_name" {
  description = "DB 이름"
  type        = string
  default     = "postgres"
}

variable "db_tier" {
  description = "PostgreSQL Flexible Server SKU"
  type        = string
  default     = "B_Standard_B1ms"
}

variable "db_disk_size_gb" {
  description = "디스크 크기 (GB). Azure 가 허용하는 크기만 가능"
  type        = number
  default     = 32

  validation {
    condition     = contains([32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768], var.db_disk_size_gb)
    error_message = "db_disk_size_gb 는 32, 64, 128 ... 32768 중 하나여야 합니다."
  }
}

variable "db_zone" {
  description = "주 DB 가용 영역"
  type        = string
  default     = "1"
}

variable "db_high_availability" {
  description = "영역 중복 HA. Burstable 등급은 지원하지 않으므로 db_tier 를 General Purpose 로 함께 올린다"
  type        = bool
  default     = false
}

variable "db_standby_zone" {
  description = "HA 대기 서버 가용 영역"
  type        = string
  default     = "2"
}

variable "db_backup_retention_days" {
  description = "백업 보관 일수"
  type        = number
  default     = 7
}

variable "db_max_connections" {
  description = "최대 연결 수. 서버를 만들 때 정해진 값이 등급 변경 뒤에도 유지되므로 db_tier 를 올리면 함께 지정한다. null 이면 건드리지 않는다"
  type        = number
  default     = null
}

variable "db_maintenance_day_of_week" {
  description = "DB 점검 요일. 0 이 일요일"
  type        = number
  default     = 0
}

variable "db_maintenance_start_hour_utc" {
  description = "DB 점검 시작 시각 (UTC). 기본값은 월요일 새벽 3시(KST)"
  type        = number
  default     = 18
}

variable "db_connection_alert_threshold" {
  description = "활성 연결 경보 기준. 등급별 최대 연결 수의 80% 정도로 잡는다. 기본값은 B1ms 사용자 연결 35개 기준"
  type        = number
  default     = 28
}

variable "health_locations" {
  description = "서비스 상태 공지를 받을 리전 표시 이름"
  type        = list(string)
  default     = ["Korea Central", "Korea South", "Global"]
}

variable "monthly_budget_usd" {
  description = "월 예산 (USD). 50, 80, 100% 를 넘으면 구독 Owner 에게 알린다. null 이면 만들지 않는다"
  type        = number
  default     = null
}

variable "vnet_cidr" {
  description = "주 VNet 대역. OCI 10.50/16, AWS 172.31/16 과 겹치면 안 된다"
  type        = string
  default     = "10.20.0.0/16"
}

variable "app_subnet_cidr" {
  description = "Container Apps 서브넷"
  type        = string
  default     = "10.20.0.0/24"
}

variable "pe_subnet_cidr" {
  description = "Private Endpoint 서브넷"
  type        = string
  default     = "10.20.1.0/27"
}

variable "gateway_subnet_cidr" {
  description = "VPN Gateway 전용 서브넷"
  type        = string
  default     = "10.20.2.0/27"
}

variable "db_peer_cidrs" {
  description = "VPN 너머에서 DB 5432 로 들어올 수 있는 대역 (OCI CDC)"
  type        = list(string)
  default     = ["10.50.0.0/16"]
}

variable "dr_enabled" {
  description = "보조 리전에 대기 자원(VNet, 레지스트리, 앱 환경)을 둘지"
  type        = bool
  default     = true
}

variable "dr_app_environment_enabled" {
  description = "보조 리전에 앱 환경을 미리 둘지. 무료 체험 구독은 환경이 1개로 제한되어 false 로 둔다"
  type        = bool
  default     = true
}

variable "dr_region" {
  description = "보조 리전"
  type        = string
  default     = "koreasouth"
}

variable "dr_vnet_cidr" {
  description = "보조 리전 VNet 대역"
  type        = string
  default     = "10.21.0.0/16"
}

variable "dr_app_subnet_cidr" {
  description = "보조 리전 Container Apps 서브넷"
  type        = string
  default     = "10.21.0.0/24"
}

variable "dr_pe_subnet_cidr" {
  description = "보조 리전 Private Endpoint 서브넷"
  type        = string
  default     = "10.21.1.0/27"
}

variable "dr_failover" {
  description = "지역 장애 때 true 로 바꿔 보조 리전에 DB 를 geo-restore 한다"
  type        = bool
  default     = false
}

variable "dr_restore_time" {
  description = "geo-restore 시점 (RFC3339 UTC). dr_failover 가 true 면 필수"
  type        = string
  default     = null
}

variable "dr_app_min_replicas" {
  description = "보조 리전 앱 최소 복제본 수. 장애 전환 때 올린다"
  type        = number
  default     = 0
}
