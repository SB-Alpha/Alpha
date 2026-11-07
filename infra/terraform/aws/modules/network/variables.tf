variable "name" {
  type = string
}

variable "container_port" {
  type = number
}

# DB 5432 에 추가로 허용할 대역. 비어 있으면 앱 보안 그룹에서 오는 접속만 허용한다.
variable "db_extra_cidrs" {
  type    = list(string)
  default = []
}

# 원본 AWS(172.31.0.0/16), OCI(10.50.0.0/16), Azure(10.20/10.21)와 겹치지 않는 대역
variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}
