provider "aws" {
  region = var.region

  # 엉뚱한 AWS 계정에 만드는 사고를 막는다. 값을 주면 다른 계정의 로그인으로는 plan/apply 가 거부된다.
  allowed_account_ids = var.aws_account_id == null ? null : [var.aws_account_id]

  default_tags {
    tags = {
      Project   = "alpha"
      ManagedBy = "terraform"
      Service   = var.service_name
    }
  }
}
