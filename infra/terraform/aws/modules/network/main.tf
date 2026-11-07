data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
  # 앱·ALB 서브넷: 10.30.0.0/24, 10.30.1.0/24
  public_cidrs = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i)]
  # DB 서브넷: 10.30.16.0/24, 10.30.17.0/24 (둘 다 10.30.16.0/20 안에 있다)
  private_cidrs = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, 16 + i)]
  db_range_cidr = cidrsubnet(var.vpc_cidr, 4, 1)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

# NAT 게이트웨이는 비용이 커서 쓰지 않는다. 앱은 공개 서브넷에서 공개 IP 로 ECR·Secrets Manager 에 나가고, 들어오는 길은 ALB 만 연다.
resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "ALB: 인터넷에서 80 포트만 받는다"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "앱: ALB 에서 오는 컨테이너 포트만 받는다"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "DB: 앱 보안 그룹(과 지정한 추가 대역)에서 오는 5432 만 받는다"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  # CDC 가 VPN 으로 들어오면 원격 대역(예: OCI)이 출발지 주소로 남는다. VPC 전체를 열면 ALB 와 공개 서브넷까지
  # 5432 에 닿으므로, 필요한 대역만 변수(cdc_allowed_cidrs)로 받는다. 기본은 비어 있다.
  dynamic "ingress" {
    for_each = length(var.db_extra_cidrs) > 0 ? [1] : []
    content {
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = var.db_extra_cidrs
    }
  }
}
