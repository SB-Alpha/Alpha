variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "name" {
  description = "secret 이름 접두사"
  type        = string
}

variable "db_password" {
  description = "database 모듈이 만든 DB 비밀번호"
  type        = string
  sensitive   = true
}

variable "secret_refs" {
  description = "환경 변수 이름 => Secret Manager secret 이름"
  type        = map(string)
  default     = {}
}
