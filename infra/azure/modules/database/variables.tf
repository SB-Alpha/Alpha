variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "vnet_id" {
  type = string
}

variable "pe_subnet_id" {
  type = string
}

variable "db_version" {
  type = string
}

variable "db_name" {
  type = string
}

variable "db_tier" {
  type = string
}

variable "db_disk_size_gb" {
  type = number
}

variable "zone" {
  type = string
}

variable "high_availability" {
  type = bool
}

variable "standby_zone" {
  type = string
}

variable "backup_retention_days" {
  type = number
}

variable "maintenance_day_of_week" {
  description = "0 이 일요일"
  type        = number
}

variable "maintenance_start_hour_utc" {
  type = number
}

variable "max_connections" {
  type = number
}
