variable "name" {
  type = string
}

variable "suffix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "vnet_cidr" {
  type = string
}

variable "app_subnet_cidr" {
  type = string
}

variable "pe_subnet_cidr" {
  type = string
}

variable "db_peer_cidrs" {
  type = list(string)
}

variable "container_port" {
  type = number
}

variable "failover" {
  type = bool
}

variable "restore_time" {
  type = string
}

variable "source_server_id" {
  type = string
}

variable "db_name" {
  type = string
}

variable "db_user" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "environment_enabled" {
  description = "앱 환경을 미리 만들지. 환경 개수가 1개로 제한된 구독에서는 끌 수밖에 없다"
  type        = bool
}

variable "deploy_app" {
  type = bool
}

variable "container_image" {
  type = string
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
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

variable "env_vars" {
  type = map(string)
}
