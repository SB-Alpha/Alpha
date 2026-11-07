variable "name" {
  type = string
}

variable "container_port" {
  type = number
}

# true 이면 ALB 의 80 포트를 CloudFront 가 원본에 접근할 때 쓰는 대역(관리형 접두사 목록)에서만 받는다.
# 인터넷 전체에서 ALB 주소로 직접 접근하는 것을 막는다.
variable "alb_cloudfront_only" {
  type    = bool
  default = false
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
