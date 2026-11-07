# ALB 는 AWS 소유 도메인(elb.amazonaws.com)을 쓰기 때문에 우리가 인증서를 받을 수 없어 https 를 직접 붙일 수 없다.
# CloudFront 는 기본 주소(xxxx.cloudfront.net)에 AWS 가 이미 가진 인증서가 붙어 있어서 도메인 없이도 https 주소가 생긴다.
# 사용자 쪽 구간만 https 이고 CloudFront 에서 ALB 까지는 AWS 내부에서 http 로 간다.

# 이 앱은 요청마다 결과가 달라서(로그인, 쓰기 요청 등) 응답을 캐시하면 안 된다. 헤더·쿠키·쿼리는 모두 원본으로 전달한다.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "this" {
  enabled         = true
  is_ipv6_enabled = true
  http_version    = "http2"
  comment         = "${var.name} https front"

  origin {
    domain_name = var.origin_domain
    origin_id   = "alb"

    # 시청자가 같은 이름의 헤더를 보내도 CloudFront 가 설정한 값으로 덮어쓴다 (원본 사용자 지정 헤더의 동작)
    custom_header {
      name  = "X-Origin-Verify"
      value = var.origin_verify_secret
    }

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only" # ALB 에는 인증서가 없어 80 포트로만 받는다
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60 # 기본 30초. 오래 걸리는 요청을 조금 더 기다린다 (60초가 상한)
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
