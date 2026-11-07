data "aws_iam_policy_document" "ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# 실행 역할: ECS 가 이미지를 받고(ECR), 로그를 쓰고, 비밀을 컨테이너 환경 변수로 넣는 데 쓴다
resource "aws_iam_role" "execution" {
  name               = "${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "${var.name}-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.secrets.json
}

# 작업 역할: 앱 코드 자신의 AWS 권한. 샘플 앱은 AWS API 를 쓰지 않아 권한이 없다.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks.json
}
