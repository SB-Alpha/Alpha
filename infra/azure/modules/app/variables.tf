variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "environment_id" {
  type = string
}

variable "container_image" {
  type = string
}

variable "registry_server" {
  type = string
}

variable "container_port" {
  type = number
}

variable "cpu" {
  type = number
}

variable "memory" {
  description = "MiB"
  type        = number
}

variable "health_check_path" {
  type = string
}

variable "min_replicas" {
  type = number
}

variable "scale_concurrent_requests" {
  type = number
}

variable "max_replicas" {
  type = number
}

variable "app_env" {
  type    = string
  default = "production"
}

variable "env_vars" {
  type    = map(string)
  default = {}
}

variable "secret_env" {
  description = "환경 변수 이름 => 버전 없는 Key Vault 비밀 ID"
  type        = map(string)
  default     = {}
}

variable "identity_id" {
  type = string
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_name" {
  type = string
}

variable "db_user" {
  type = string
}

variable "db_password_secret_id" {
  type = string
}
