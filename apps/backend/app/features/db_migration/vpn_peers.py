"""클라우드별 VPN 차이. 공통 절차는 이 어댑터의 같은 메서드만 호출한다."""

from typing import Protocol

from .vpn import OciSettings
from .vpn_plan import PeerTunnel, VpnError, VpnLeg, public_ips

PLACEHOLDER_IPS = ("1.1.1.1", "1.1.1.2")


class TerraformPort(Protocol):
    def apply(self, role: str, values: dict) -> None: ...
    def refresh(self, role: str, values: dict) -> None: ...
    def destroy(self, role: str, values: dict) -> None: ...
    def output(self, role: str, name: str): ...


class PeerAdapter(Protocol):
    root: str
    asn: str

    def validate(self, leg: VpnLeg) -> None: ...
    def credentials(self, values: dict[str, str]) -> dict[str, str]: ...
    def variables(self, leg: VpnLeg, name: str, settings: OciSettings, psk: dict, oci_ips: dict) -> dict: ...
    def peers(self, tf: TerraformPort, leg: VpnLeg) -> list[PeerTunnel]: ...


def _required(leg: VpnLeg, *names: str) -> None:
    if any(not leg.network.get(name) for name in names):
        raise VpnError("invalid_plan", f"{leg.role}: missing network fields required by the VPN adapter")


class AwsPeer:
    root = "aws-vpn"
    asn = "64512"

    def validate(self, leg: VpnLeg) -> None:
        _required(leg, "vpc_id", "route_table_ids", "db_security_group_id")

    def credentials(self, values: dict[str, str]) -> dict[str, str]:
        env = {"AWS_ACCESS_KEY_ID": values["aws_access_key_id"], "AWS_SECRET_ACCESS_KEY": values["aws_secret_access_key"]}
        if values.get("aws_session_token"):
            env["AWS_SESSION_TOKEN"] = values["aws_session_token"]
        if not all(env.values()):
            raise ValueError
        return env

    def variables(self, leg: VpnLeg, name: str, settings: OciSettings, psk: dict, oci_ips: dict) -> dict:
        return {
            "region": leg.region, "name": name, "vpc_id": leg.network["vpc_id"],
            "route_table_ids": leg.network["route_table_ids"], "db_security_group_id": leg.network["db_security_group_id"],
            "db_port": leg.db_port, "oci_cidr": settings.node_subnet_cidr,
            "tunnels": [{"oci_ip": oci_ips.get(key, PLACEHOLDER_IPS[i]), "inside_cidr": leg.inside_cidr(i)}
                        for i, key in enumerate(leg.keys)],
            "shared_secrets": [psk[key] for key in leg.keys],
        }

    def peers(self, tf: TerraformPort, leg: VpnLeg) -> list[PeerTunnel]:
        values = tf.output(leg.role, "tunnels")
        if values is None:
            return []
        try:
            ips = public_ips([t["aws_ip"] for t in values], 2)
            if any(str(t["amazon_asn"]) != "64512" or t["aws_bgp_ip"] != leg.peer_bgp_ip(i)
                   or t["oci_bgp_ip"] != leg.oracle_bgp_ip(i) for i, t in enumerate(values)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise VpnError("invalid_output", f"{leg.role}: AWS returned unexpected BGP settings") from None
        return [PeerTunnel(key, ips[i], "64512") for i, key in enumerate(leg.keys)]


class GcpPeer:
    root = "gcp"
    asn = "65010"

    def validate(self, leg: VpnLeg) -> None:
        _required(leg, "project_id", "vpc_name", "psa_range_cidr")

    def credentials(self, values: dict[str, str]) -> dict[str, str]:
        if not values["credentials_json"]:
            raise ValueError
        return {"GOOGLE_CREDENTIALS": values["credentials_json"]}

    def variables(self, leg: VpnLeg, name: str, settings: OciSettings, psk: dict, oci_ips: dict) -> dict:
        linked = all(key in oci_ips for key in leg.keys)
        return {
            "project": leg.network["project_id"], "region": leg.region, "network": leg.network["vpc_name"], "name": name,
            "advertised_ip_ranges": leg.network.get("psa_range_cidrs") or [leg.network["psa_range_cidr"]],
            "oci_tunnels": [{"oci_ip": oci_ips[key], "gcp_bgp_ip": f"{leg.peer_bgp_ip(i)}/30",
                             "oci_bgp_ip": leg.oracle_bgp_ip(i)} for i, key in enumerate(leg.keys)] if linked else [],
            "shared_secrets": [psk[key] for key in leg.keys] if linked else [],
        }

    def peers(self, tf: TerraformPort, leg: VpnLeg) -> list[PeerTunnel]:
        values = tf.output(leg.role, "ha_vpn_interface_ips")
        if values is None:
            return []
        ips = public_ips(values, 2)
        if str(tf.output(leg.role, "router_asn")) != "65010":
            raise VpnError("invalid_output", f"{leg.role}: GCP returned an unexpected router ASN")
        return [PeerTunnel(key, ips[i], "65010") for i, key in enumerate(leg.keys)]


class AzurePeer:
    """Active-Active 게이트웨이의 인스턴스 2개가 OCI 터널 2개와 하나씩 BGP로 연결된다.

    GatewaySubnet은 VNet 대역을 정하는 일이라 만들지 않는다. 없으면 자원을 만들기 전에 거절한다.
    """

    root = "azure-vpn"
    asn = "65515"
    # Azure는 OCI 기본 PFS 그룹 5를 제안하지 않아 Phase 2가 맞지 않는다.
    phase_two_dh_group = "GROUP14"

    def validate(self, leg: VpnLeg) -> None:
        _required(leg, "resource_group", "vnet_name", "has_gateway_subnet")

    def credentials(self, values: dict[str, str]) -> dict[str, str]:
        env = {"ARM_TENANT_ID": values["tenant_id"], "ARM_CLIENT_ID": values["client_id"],
               "ARM_CLIENT_SECRET": values["client_secret"], "ARM_SUBSCRIPTION_ID": values["subscription_id"]}
        if not all(env.values()):
            raise ValueError
        return env

    def variables(self, leg: VpnLeg, name: str, settings: OciSettings, psk: dict, oci_ips: dict) -> dict:
        linked = all(key in oci_ips for key in leg.keys)
        return {
            "resource_group": leg.network["resource_group"], "location": leg.region,
            "vnet_name": leg.network["vnet_name"], "name": name, "gateway_asn": int(self.asn),
            "gateway_bgp_ips": [leg.peer_bgp_ip(i) for i in range(2)],
            "oci_tunnels": [{"oci_ip": oci_ips[key], "oci_bgp_ip": leg.oracle_bgp_ip(i)}
                            for i, key in enumerate(leg.keys)] if linked else [],
            "shared_secrets": [psk[key] for key in leg.keys] if linked else [],
        }

    def peers(self, tf: TerraformPort, leg: VpnLeg) -> list[PeerTunnel]:
        values = tf.output(leg.role, "gateway_public_ips")
        if values is None:
            return []
        ips = public_ips(values, 2)
        expected = [leg.peer_bgp_ip(i) for i in range(2)]
        if str(tf.output(leg.role, "gateway_asn")) != self.asn or tf.output(leg.role, "gateway_bgp_ips") != expected:
            raise VpnError("invalid_output", f"{leg.role}: Azure returned unexpected BGP settings")
        return [PeerTunnel(key, ips[i], self.asn, self.phase_two_dh_group) for i, key in enumerate(leg.keys)]


ADAPTERS: dict[str, PeerAdapter] = {"aws": AwsPeer(), "gcp": GcpPeer(), "azure": AzurePeer()}


def adapters_for(legs: tuple[VpnLeg, VpnLeg], registry: dict[str, PeerAdapter] | None = None) -> dict[str, PeerAdapter]:
    registry = ADAPTERS if registry is None else registry
    result = {}
    for leg in legs:
        if leg.cloud not in registry:
            raise VpnError("unsupported_cloud", f"{leg.role}: VPN adapter is not implemented for this cloud")
        adapter = registry[leg.cloud]
        adapter.validate(leg)
        result[leg.role] = adapter
    return result
