# AWS 쪽 VPN: VGW 1개 + OCI 터널마다 고객 게이트웨이·VPN 연결 1개.
# AWS VPN 연결도 터널을 2개 만들지만 OCI 터널 1개와 짝이 되는 tunnel1만 쓴다.
#
# 양쪽이 서로의 공인 IP를 알아야 해서 Oracle 안내대로 2번에 나눠 적용한다.
#   1. oci_ip에 임시 IP를 넣어 apply → tunnel1 공인 IP 확보 (OCI CPE에 넣는다)
#   2. OCI 터널 IP로 바꿔 apply → 고객 게이트웨이만 교체된다 (VPN 연결은 그대로)

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "vpc_id" {
  type = string
}

variable "route_table_ids" {
  description = "RDS 서브넷이 쓰는 라우팅 테이블. OCI 대역 경로를 BGP로 전파받는다"
  type        = list(string)
}

variable "amazon_side_asn" {
  type    = number
  default = 64512
}

variable "oci_asn" {
  type    = number
  default = 31898
}

variable "tunnels" {
  description = "OCI 터널마다 1개. oci_ip는 OCI 쪽 oracle_tunnel_ip 출력값"
  type = list(object({
    oci_ip      = string
    inside_cidr = string # 169.254.x.x/30, AWS가 .1, OCI가 .2. 169.254.10.0~19.255는 OCI가 거부한다
  }))
}

variable "shared_secrets" {
  description = "tunnels와 같은 순서. OCI 쪽과 같은 값"
  type        = list(string)
  sensitive   = true
}

variable "db_security_group_id" {
  description = "RDS 보안 그룹. 비우면 규칙을 만들지 않는다"
  type        = string
  default     = null
}

variable "oci_cidr" {
  type = string
}

variable "db_port" {
  type = number
}

resource "aws_vpn_gateway" "this" {
  vpc_id          = var.vpc_id
  amazon_side_asn = var.amazon_side_asn

  tags = {
    Name = var.name
  }
}

resource "aws_customer_gateway" "oci" {
  count = length(var.tunnels)

  bgp_asn    = var.oci_asn
  ip_address = var.tunnels[count.index].oci_ip
  type       = "ipsec.1"

  tags = {
    Name = "${var.name}-oci-${count.index}"
  }

  # VPN 연결이 쓰는 중에는 지울 수 없으므로 새 게이트웨이로 옮긴 뒤 지운다.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpn_connection" "oci" {
  count = length(var.tunnels)

  vpn_gateway_id        = aws_vpn_gateway.this.id
  customer_gateway_id   = aws_customer_gateway.oci[count.index].id
  type                  = "ipsec.1"
  static_routes_only    = false
  tunnel1_inside_cidr   = var.tunnels[count.index].inside_cidr
  tunnel1_preshared_key = var.shared_secrets[count.index]
  tunnel1_ike_versions  = ["ikev2"]

  tags = {
    Name = "${var.name}-oci-${count.index}"
  }
}

resource "aws_vpn_gateway_route_propagation" "rds" {
  for_each = toset(var.route_table_ids)

  vpn_gateway_id = aws_vpn_gateway.this.id
  route_table_id = each.value
}

resource "aws_vpc_security_group_ingress_rule" "oci_postgres" {
  count = var.db_security_group_id == null ? 0 : 1

  security_group_id = var.db_security_group_id
  cidr_ipv4         = var.oci_cidr
  ip_protocol       = "tcp"
  from_port         = var.db_port
  to_port           = var.db_port
  description       = "OCI CDC over VPN"
}

output "tunnels" {
  description = "OCI 쪽에 넘길 값: cpe_ip = aws_ip, peer_asn = amazon_asn"
  value = [for c in aws_vpn_connection.oci : {
    aws_ip     = c.tunnel1_address
    aws_bgp_ip = c.tunnel1_vgw_inside_address
    oci_bgp_ip = c.tunnel1_cgw_inside_address
    amazon_asn = var.amazon_side_asn
  }]
}
