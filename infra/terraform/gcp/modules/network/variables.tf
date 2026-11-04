variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "GCP 리전"
  type        = string
  default     = "asia-northeast3"
}

variable "name" {
  description = "VPC 이름 접두사"
  type        = string
}

variable "subnet_cidr" {
  description = "서브넷 CIDR 블록"
  type        = string
  default     = "10.10.0.0/24"
}