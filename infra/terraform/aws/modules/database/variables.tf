variable "name" {
  type = string
}

variable "db_version" {
  type = string
}

variable "db_name" {
  type = string
}

variable "db_user" {
  type = string
}

variable "instance_class" {
  type = string
}

variable "disk_size_gb" {
  type = number
}

variable "logical_decoding" {
  type = bool
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}
