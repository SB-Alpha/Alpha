locals {
  plain_env = merge(var.env_vars, {
    APP_ENV    = var.app_env
    PORT       = tostring(var.container_port)
    PGHOST     = var.db_host
    PGPORT     = tostring(var.db_port)
    PGDATABASE = var.db_name
    PGUSER     = var.db_user
    PGSSLMODE  = "require"
  })

  secret_env = merge(var.secret_env, {
    PGPASSWORD = var.db_password_arn
  })
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name}"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "this" {
  name = var.name
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.cpu * 1024)
  memory                   = tostring(var.memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name         = var.name
      image        = var.container_image
      essential    = true
      portMappings = [{ containerPort = var.container_port, protocol = "tcp" }]
      environment  = [for k, v in local.plain_env : { name = k, value = v }]
      secrets      = [for k, arn in local.secret_env : { name = k, valueFrom = arn }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "app"
        }
      }
    }
  ])
}

resource "aws_lb" "this" {
  name               = var.name
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  security_groups    = [var.alb_security_group_id]
}

resource "aws_lb_target_group" "this" {
  name                 = var.name
  port                 = var.container_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    matcher             = "200-399"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# 인증서와 도메인이 없어서 HTTP(80)로 연다. HTTPS 는 앞에 둔 CloudFront 가 맡는다 (루트의 https_front).
# origin_guard 가 켜져 있으면 기본 응답은 403 이고, CloudFront 가 붙이는 비밀 헤더가 맞는 요청만 아래 규칙으로 앱에 전달한다.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = var.origin_guard ? "fixed-response" : "forward"
    target_group_arn = var.origin_guard ? null : aws_lb_target_group.this.arn

    dynamic "fixed_response" {
      for_each = var.origin_guard ? [1] : []
      content {
        content_type = "text/plain"
        message_body = "forbidden"
        status_code  = "403"
      }
    }
  }
}

resource "aws_lb_listener_rule" "origin_verify" {
  count        = var.origin_guard ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [var.origin_verify_secret]
    }
  }
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = true # NAT 없이 ECR·Secrets Manager 에 나가려고 공개 IP 를 붙인다. 들어오는 길은 보안 그룹이 ALB 로 막는다.
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = var.name
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http]
}
