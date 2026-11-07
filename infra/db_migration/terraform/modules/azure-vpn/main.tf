# Azure 쪽 VPN: Active-Active VPN Gateway(공인 IP 2개) + OCI 터널 2개.
# 인스턴스 0은 OCI 터널 0과, 인스턴스 1은 OCI 터널 1과 BGP로 연결된다.
#
# 만드는 순서 (OCI와 서로의 IP가 필요하다):
#   1. oci_tunnels = [] 로 apply → gateway_public_ips 확보 (게이트웨이 생성에 30분 이상 걸릴 수 있다)
#   2. OCI 쪽 apply (cpe_ip = 위 IP) → OCI 터널 IP 확보
#   3. oci_tunnels 를 채워 다시 apply → Local Network Gateway·연결 생성
#
# DB 서브넷의 NSG는 이 모듈이 바꾸지 않는다. CDC 노드 대역은 NSG에서 미리 허용해 둔다.

terraform {
  required_providers {
    azurerm = {
      source = "hashicorp/azurerm"
    }
  }
}

variable "resource_group" {
  description = "VNet이 있는 리소스 그룹. VPN 자원도 여기에 만든다"
  type        = string
}

variable "location" {
  type = string
}

variable "vnet_name" {
  description = "DB의 Private Endpoint가 있는 VNet. GatewaySubnet이 있어야 한다"
  type        = string
}

variable "name" {
  type    = string
  default = "alpha-cdc"
}

variable "sku" {
  type    = string
  default = "VpnGw1AZ"
}

variable "gateway_asn" {
  type    = number
  default = 65515
}

variable "oci_asn" {
  type    = number
  default = 31898
}

variable "gateway_bgp_ips" {
  description = "인스턴스 0, 1의 BGP 주소. Azure가 허용하는 169.254.21.0~22.255 안의 값"
  type        = list(string)

  validation {
    condition     = length(var.gateway_bgp_ips) == 2
    error_message = "gateway_bgp_ips는 인스턴스마다 하나씩 2개다."
  }
}

variable "oci_tunnels" {
  description = "인스턴스 0, 1 순서. oci_ip는 OCI 쪽 oracle_tunnel_ip 출력값"
  type = list(object({
    oci_ip     = string
    oci_bgp_ip = string # 169.254.21.2 (접두 길이 없이)
  }))
  default = []

  validation {
    condition     = contains([0, 2], length(var.oci_tunnels))
    error_message = "oci_tunnels는 비어 있거나(1단계) 2개다."
  }
}

variable "shared_secrets" {
  description = "oci_tunnels와 같은 순서. OCI 쪽과 같은 값"
  type        = list(string)
  default     = []
  sensitive   = true
}

locals {
  ip_configurations = ["ipconfig-0", "ipconfig-1"]
}

data "azurerm_subnet" "gateway" {
  name                 = "GatewaySubnet"
  resource_group_name  = var.resource_group
  virtual_network_name = var.vnet_name
}

resource "azurerm_public_ip" "gateway" {
  count = 2

  name                = "${var.name}-${count.index}"
  resource_group_name = var.resource_group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
}

resource "azurerm_virtual_network_gateway" "this" {
  name                = var.name
  resource_group_name = var.resource_group
  location            = var.location
  type                = "Vpn"
  vpn_type            = "RouteBased"
  sku                 = var.sku
  active_active       = true
  bgp_enabled         = true

  dynamic "ip_configuration" {
    for_each = local.ip_configurations
    content {
      name                          = ip_configuration.value
      public_ip_address_id          = azurerm_public_ip.gateway[ip_configuration.key].id
      private_ip_address_allocation = "Dynamic"
      subnet_id                     = data.azurerm_subnet.gateway.id
    }
  }

  bgp_settings {
    asn = var.gateway_asn

    dynamic "peering_addresses" {
      for_each = local.ip_configurations
      content {
        ip_configuration_name = peering_addresses.value
        apipa_addresses       = [var.gateway_bgp_ips[peering_addresses.key]]
      }
    }
  }
}

resource "azurerm_local_network_gateway" "oci" {
  count = length(var.oci_tunnels)

  name                = "${var.name}-oci-${count.index}"
  resource_group_name = var.resource_group
  location            = var.location
  gateway_address     = var.oci_tunnels[count.index].oci_ip

  bgp_settings {
    asn                 = var.oci_asn
    bgp_peering_address = var.oci_tunnels[count.index].oci_bgp_ip
  }
}

resource "azurerm_virtual_network_gateway_connection" "oci" {
  count = length(var.oci_tunnels)

  name                       = "${var.name}-oci-${count.index}"
  resource_group_name        = var.resource_group
  location                   = var.location
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.this.id
  local_network_gateway_id   = azurerm_local_network_gateway.oci[count.index].id
  connection_protocol        = "IKEv2"
  bgp_enabled                = true
  shared_key                 = var.shared_secrets[count.index]

  custom_bgp_addresses {
    primary   = var.gateway_bgp_ips[0]
    secondary = var.gateway_bgp_ips[1]
  }
}

output "gateway_public_ips" {
  description = "OCI 쪽 cpe_ip로 넣는 Azure 공인 IP 2개 (인스턴스 0, 1)"
  value       = azurerm_public_ip.gateway[*].ip_address
}

output "gateway_asn" {
  value = azurerm_virtual_network_gateway.this.bgp_settings[0].asn
}

output "gateway_bgp_ips" {
  description = "실제 게이트웨이에 설정된 BGP 주소 (인스턴스 0, 1)"
  value = [
    for name in local.ip_configurations : one(flatten([
      for peering in azurerm_virtual_network_gateway.this.bgp_settings[0].peering_addresses :
      tolist(peering.apipa_addresses) if peering.ip_configuration_name == name
    ]))
  ]
}
