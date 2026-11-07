# 저장소 이름 = 서비스 이름. 백엔드가 이미지를 <registry_url>/<service_name> 으로 복사한다.
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # 임시 데모 환경이라 이미지가 남아 있어도 destroy 가능하게 둠

  image_scanning_configuration {
    scan_on_push = true
  }
}
