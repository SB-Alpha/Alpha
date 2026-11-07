variable "name" {
  type = string
}

variable "container_port" {
  type = number
}

# 원본 AWS(172.31.0.0/16), OCI(10.50.0.0/16), Azure(10.20/10.21)와 겹치지 않는 대역
variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}
