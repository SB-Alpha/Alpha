variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "name" {
  description = "서비스 계정 이름 접두사"
  type        = string
}

variable "create_vpn_key" {
  description = "VPN 서비스 계정 키를 발급할지. 조직 정책(iam.disableServiceAccountKeyCreation)이 막고 있으면 false"
  type        = bool
  default     = false
}

variable "secret_ids" {
  description = "Cloud Run 서비스 계정이 읽을 수 있어야 하는 Secret Manager secret 이름 목록"
  type        = list(string)
  default     = []
}