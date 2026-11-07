variable "aws_account_id" {
  description = "목적지 AWS 계정 ID(12자리). 주면 이 계정이 아닌 로그인으로는 실행되지 않는다. 비우면 확인하지 않는다"
  type        = string
  default     = null

  validation {
    condition     = var.aws_account_id == null || can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id 는 12자리 숫자여야 합니다."
  }
}

variable "region" {
  description = "AWS 리전"
  type        = string
  default     = "ap-northeast-2"
}

# 아래부터는 MigrationPlan.target_infra 와 이름이 1:1로 대응하는 입력입니다.
variable "service_name" {
  description = "서비스 이름 (ECR 저장소·ECS 서비스 이름의 기준)"
  type        = string
}

variable "source_image" {
  description = "원본 이미지 주소. 목적지 저장소로의 복사는 Terraform 밖(백엔드)에서 처리"
  type        = string
  default     = null
}

variable "deploy_app" {
  description = "ECS 앱을 배포할지. db_migration 이 streaming 에 들어가 테이블이 만들어진 뒤에 true 로 바꿔 두 번째 apply 를 한다"
  type        = bool
  default     = false
}

variable "container_image" {
  description = "ECS 가 실행할 ECR 이미지 주소. 비우면 registry 주소 기준 기본값을 씀"
  type        = string
  default     = null
}

variable "container_port" {
  description = "컨테이너 포트"
  type        = number
  default     = 8080
}

variable "cpu" {
  description = "vCPU 개수. Fargate 가 받는 값: 0.25, 0.5, 1, 2, 4"
  type        = number
  default     = 0.5

  validation {
    condition     = contains([0.25, 0.5, 1, 2, 4], var.cpu)
    error_message = "Fargate 의 cpu 는 0.25, 0.5, 1, 2, 4 중 하나여야 합니다."
  }
}

variable "memory" {
  description = "메모리 (MiB). cpu 마다 가능한 범위가 정해져 있다 (예: 0.5 vCPU 는 1024~4096, 1 vCPU 는 2048~8192)"
  type        = number
  default     = 1024
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
  description = "환경 변수 이름 => Secrets Manager 비밀 이름"
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
  description = "DB 사용자 이름. RDS 는 Terraform 만으로 사용자를 더 만들 수 없어 이 사용자가 앱과 관리자(db_migration)를 겸한다"
  type        = string
  default     = "alpha"
}

variable "db_instance_class" {
  description = "RDS 인스턴스 등급"
  type        = string
  default     = "db.t4g.small"
}

variable "db_disk_size_gb" {
  description = "RDS 디스크 크기 (GB)"
  type        = number
  default     = 20
}

variable "db_logical_decoding" {
  description = "논리 복제(CDC) 사용 여부"
  type        = bool
  default     = true
}
