resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.private_subnet_ids
}

# 논리 복제(CDC)와 암호화 접속 강제. rds.logical_replication 은 재시작이 필요한 설정이라 인스턴스를 만들 때 함께 적용한다.
resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg"
  family = "postgres${var.db_version}"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  dynamic "parameter" {
    for_each = var.logical_decoding ? [1] : []
    content {
      name         = "rds.logical_replication"
      value        = "1"
      apply_method = "pending-reboot"
    }
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = var.db_version
  instance_class = var.instance_class

  allocated_storage = var.disk_size_gb
  storage_type      = "gp3"
  storage_encrypted = true

  # 기본 DB(postgres)는 인스턴스에 이미 있으므로 다른 이름일 때만 만든다
  db_name  = var.db_name == "postgres" ? null : var.db_name
  username = var.db_user
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = var.security_group_ids
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 1
  skip_final_snapshot     = true  # 임시 데모 환경이라 destroy 때 스냅샷을 남기지 않음
  deletion_protection     = false # 임시 데모 환경이라 destroy 가능하게 둠
  apply_immediately       = true
}
