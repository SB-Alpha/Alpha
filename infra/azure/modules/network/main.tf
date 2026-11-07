resource "azurerm_virtual_network" "this" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = [var.vnet_cidr]
}

resource "azurerm_subnet" "apps" {
  name                 = "snet-apps"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.app_subnet_cidr]

  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "pe" {
  name                 = "snet-pe"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.pe_subnet_cidr]

  # 켜야 Private Endpoint 로 가는 트래픽에 NSG 가 적용된다
  private_endpoint_network_policies = "Enabled"
}

# 이름이 정해져 있고 NSG 를 붙일 수 없는 서브넷. VPN Gateway 는 이전 기간에만 만든다.
resource "azurerm_subnet" "gateway" {
  count                = var.gateway_subnet_cidr == null ? 0 : 1
  name                 = "GatewaySubnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.gateway_subnet_cidr]
}

# 외부 공개 환경의 인바운드는 서브넷이 아니라 관리 리소스 그룹의 공인 IP 로 들어와 NSG 로 막을 수 없다.
# 아웃바운드 제어를 붙일 자리로 기본 규칙만 둔다.
resource "azurerm_network_security_group" "apps" {
  name                = "nsg-apps"
  resource_group_name = var.resource_group_name
  location            = var.location
}

resource "azurerm_network_security_group" "pe" {
  name                = "nsg-pe"
  resource_group_name = var.resource_group_name
  location            = var.location

  security_rule {
    name                       = "allow-postgres-from-apps"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5432"
    source_address_prefix      = var.app_subnet_cidr
    destination_address_prefix = var.pe_subnet_cidr
  }

  # DB 로 들어오는 외부 경로는 VPN 너머의 CDC 뿐이다
  security_rule {
    name                       = "allow-postgres-from-vpn-peers"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5432"
    source_address_prefixes    = var.db_peer_cidrs
    destination_address_prefix = var.pe_subnet_cidr
  }

  security_rule {
    name                       = "deny-all-inbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "apps" {
  subnet_id                 = azurerm_subnet.apps.id
  network_security_group_id = azurerm_network_security_group.apps.id
}

resource "azurerm_subnet_network_security_group_association" "pe" {
  subnet_id                 = azurerm_subnet.pe.id
  network_security_group_id = azurerm_network_security_group.pe.id
}
