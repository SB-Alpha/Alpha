# 노드에는 공인 IP가 없다. 운영 접속은 OCI Bastion 포트 포워딩 세션으로만 한다.
# 세션은 최대 3시간이라, 만료되면 다시 apply해 새로 만든다.

resource "oci_bastion_bastion" "this" {
  count = length(var.admin_cidrs) > 0 ? 1 : 0

  compartment_id               = var.compartment_id
  bastion_type                 = "STANDARD"
  name                         = replace(var.name, "-", "")
  target_subnet_id             = module.network.subnet_id
  client_cidr_block_allow_list = var.admin_cidrs
  max_session_ttl_in_seconds   = 10800
}

resource "oci_bastion_session" "ssh" {
  count = length(var.admin_cidrs) > 0 && var.ssh_sessions ? var.node_count : 0

  bastion_id             = oci_bastion_bastion.this[0].id
  display_name           = "${var.name}-${count.index + 1}"
  session_ttl_in_seconds = 10800

  key_details {
    public_key_content = var.ssh_public_key
  }

  target_resource_details {
    session_type                       = "PORT_FORWARDING"
    target_resource_private_ip_address = module.nodes.node_private_ips[count.index]
    target_resource_port               = 22
  }
}

output "ssh_tunnel_commands" {
  description = "<privateKey>, <localPort>를 바꿔 실행하면 localhost:<localPort>가 노드 22번으로 이어진다"
  value       = [for s in oci_bastion_session.ssh : s.ssh_metadata["command"]]
}
