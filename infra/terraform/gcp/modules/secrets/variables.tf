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

variable "protect_ingress" {
  description = "Worker 시크릿을 Secret Manager 에 만들지"
  type        = bool
  default     = false
}

variable "worker_secret" {
  description = "Cloudflare Worker 가 주입하는 사전 공유 시크릿 값 (protect_ingress 일 때만 쓴다)"
  type        = string
  default     = ""
  sensitive   = true
}
