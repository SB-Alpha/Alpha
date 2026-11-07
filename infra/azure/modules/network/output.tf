output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "vnet_name" {
  value = azurerm_virtual_network.this.name
}

output "app_subnet_id" {
  # NSG 를 붙인 뒤에 앱 환경을 만들도록 연결 자원의 ID 를 넘긴다
  value = azurerm_subnet_network_security_group_association.apps.subnet_id
}

output "pe_subnet_id" {
  value = azurerm_subnet_network_security_group_association.pe.subnet_id
}

output "gateway_subnet_id" {
  value = try(azurerm_subnet.gateway[0].id, null)
}

output "subnet_cidrs" {
  value = azurerm_subnet.apps.address_prefixes
}

output "pe_subnet_cidr" {
  value = var.pe_subnet_cidr
}
