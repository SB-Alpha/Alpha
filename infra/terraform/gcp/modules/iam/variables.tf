variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "name" {
  description = "서비스 계정 이름 접두사"
  type        = string
}

variable "secret_ids" {
  description = "Cloud Run 서비스 계정이 읽을 수 있어야 하는 Secret Manager secret 이름 목록"
  type        = list(string)
  default     = []
}