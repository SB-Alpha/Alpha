variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "Cloud Run 을 만들 리전"
  type        = string
}

variable "name" {
  description = "Cloud Run 서비스 이름"
  type        = string
}

variable "container_image" {
  description = "Artifact Registry 의 컨테이너 이미지 주소"
  type        = string
}

variable "container_port" {
  description = "컨테이너 포트"
  type        = number
}

variable "cpu" {
  description = "vCPU 개수"
  type        = number
}

variable "memory" {
  description = "메모리 (MiB)"
  type        = number
}

variable "health_check_path" {
  description = "시작 확인(startup probe)에 쓸 경로"
  type        = string
}

variable "env_vars" {
  description = "secret 이 아닌 환경 변수 (이름 => 값)"
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = "secret 환경 변수 (환경 변수 이름 => Secret Manager 비밀 이름)"
  type        = map(string)
  default     = {}
}

variable "service_account_email" {
  description = "Cloud Run 이 사용할 서비스 계정 이메일"
  type        = string
}

variable "network_id" {
  description = "Cloud Run 이 DB 에 접근할 VPC"
  type        = string
}

variable "subnetwork_id" {
  description = "Cloud Run 이 사용할 서브넷"
  type        = string
}

variable "db_host" {
  description = "Cloud SQL 사설 IP"
  type        = string
}

variable "db_port" {
  description = "PostgreSQL 포트"
  type        = number
}

variable "db_name" {
  description = "DB 이름"
  type        = string
}

variable "db_user" {
  description = "DB 사용자 이름"
  type        = string
}

variable "db_password_secret_id" {
  description = "DB 비밀번호가 담긴 Secret Manager 비밀 이름"
  type        = string
}

variable "app_env" {
  description = "응답 헤더 X-Served-By 에 찍힐 처리 환경 표시"
  type        = string
  default     = "gcp"
}
