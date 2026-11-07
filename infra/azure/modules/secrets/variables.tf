variable "name" {
  description = "Key Vault 이름 (kv- 를 붙여 24자 이하)"
  type        = string
}

variable "secret_prefix" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "secret_refs" {
  description = "환경 변수 이름 => Key Vault 비밀 이름"
  type        = map(string)
  default     = {}
}
