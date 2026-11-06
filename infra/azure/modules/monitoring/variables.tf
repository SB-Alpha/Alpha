variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "subscription_id" {
  type = string
}

variable "workspace_id" {
  type = string
}

variable "db_server_id" {
  type = string
}

variable "key_vault_id" {
  type = string
}

variable "registry_id" {
  type = string
}

variable "db_burstable" {
  type = bool
}

variable "db_connection_alert_threshold" {
  type = number
}

variable "health_locations" {
  description = "서비스 상태 공지를 받을 리전 표시 이름"
  type        = list(string)
}

variable "monthly_budget_usd" {
  type = number
}
