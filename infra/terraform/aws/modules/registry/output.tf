# repository_url 은 <계정>.dkr.ecr.<리전>.amazonaws.com/<이름>. 호스트만 돌려준다 (GCP·Azure 의 registry_url 과 쓰임이 같다).
output "registry_url" {
  value = element(split("/", aws_ecr_repository.this.repository_url), 0)
}

output "repository_arn" {
  value = aws_ecr_repository.this.arn
}
