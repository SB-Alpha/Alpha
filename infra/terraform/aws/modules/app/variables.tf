variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "container_image" {
  type = string
}

variable "container_port" {
  type = number
}

variable "cpu" {
  description = "vCPU 개수 (Fargate: 0.25, 0.5, 1, 2, 4)"
  type        = number
}

variable "memory" {
  description = "메모리 (MiB)"
  type        = number
}

variable "health_check_path" {
  type = string
}

variable "env_vars" {
  type    = map(string)
  default = {}
}

# 환경 변수 이름 => Secrets Manager 비밀 ARN
variable "secret_env" {
  type    = map(string)
  default = {}
}

variable "app_env" {
  description = "응답 헤더 X-Served-By 에 찍힐 처리 환경 표시"
  type        = string
  default     = "aws"
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "alb_security_group_id" {
  type = string
}

variable "app_security_group_id" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_name" {
  type = string
}

variable "db_user" {
  type = string
}

variable "db_password_arn" {
  type = string
}
