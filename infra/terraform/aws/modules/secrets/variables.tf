variable "name" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "secret_refs" {
  type    = map(string)
  default = {}
}
