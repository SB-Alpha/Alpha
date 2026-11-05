variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "GCP 리전"
  type        = string
  default     = "asia-northeast3"
}

# 아래부터는 MigrationPlan.target_infra 와 이름이 1:1로 대응하는 입력입니다.
# 값이 없는 키는 tfvars 에서 빠져서 오므로, 선택 항목은 기본값을 둡니다.
variable "service_name" {
  description = "서비스 이름 (저장소·Cloud Run 서비스 이름의 기준)"
  type        = string
}

variable "source_image" {
  description = "원본 ECR 이미지 주소. Artifact Registry 복사는 Terraform 밖에서 처리"
  type        = string
  default     = null
}

variable "deploy_app" {
  description = "Cloud Run 앱을 배포할지. db_migration 이 streaming 에 들어가 테이블이 만들어진 뒤에 true 로 바꿔 두 번째 apply 를 한다"
  type        = bool
  default     = false
}

variable "create_vpn_key" {
  description = "VPN 서비스 계정 키를 발급할지. 조직 정책이 키 발급을 막고 있으면 false"
  type        = bool
  default     = false
}

variable "container_image" {
  description = "Cloud Run 이 실행할 Artifact Registry 이미지 주소. 비우면 registry 주소 기준 기본값을 씀"
  type        = string
  default     = null
}

variable "container_port" {
  description = "컨테이너 포트"
  type        = number
  default     = 8080
}

variable "cpu" {
  description = "vCPU 개수"
  type        = number
  default     = 1
}

variable "memory" {
  description = "메모리 (MiB)"
  type        = number
  default     = 512
}

variable "health_check_path" {
  description = "상태 확인 경로"
  type        = string
  default     = "/health/ready"
}

variable "env_vars" {
  description = "비밀이 아닌 환경 변수 (이름 => 값). PG*, APP_ENV, PORT 는 Terraform 이 채움"
  type        = map(string)
  default     = {}
}

variable "secret_refs" {
  description = "환경 변수 이름 => Secret Manager 비밀 이름"
  type        = map(string)
  default     = {}
}

variable "db_version" {
  description = "PostgreSQL 메이저 버전"
  type        = string
  default     = "16"
}

variable "db_name" {
  description = "DB 이름 (기본값은 샘플 앱의 DB 이름)"
  type        = string
  default     = "postgres"
}

variable "db_user" {
  description = "앱이 쓰는 DB 사용자 이름"
  type        = string
  default     = "alpha"
}

variable "db_tier" {
  description = "Cloud SQL 머신 등급"
  type        = string
  default     = "db-g1-small"
}

variable "db_disk_size_gb" {
  description = "Cloud SQL 디스크 크기 (GB)"
  type        = number
  default     = 20
}

variable "db_logical_decoding" {
  description = "논리 복제(CDC) 사용 여부"
  type        = bool
  default     = true
}
