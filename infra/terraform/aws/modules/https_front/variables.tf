variable "name" {
  type = string
}

# 앞에 둘 ALB 의 DNS 이름 (xxxx.ap-northeast-2.elb.amazonaws.com)
variable "origin_domain" {
  type = string
}

# CloudFront 가 ALB 로 보내는 모든 요청에 X-Origin-Verify 헤더로 붙이는 비밀 값. ALB 가 이 값이 맞는 요청만 앱으로 전달한다.
variable "origin_verify_secret" {
  type      = string
  sensitive = true
}
