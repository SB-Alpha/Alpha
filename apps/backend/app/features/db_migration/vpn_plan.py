"""VPN 구간은 클라우드 이름 대신 source/target 역할로 식별한다."""

import copy
import ipaddress
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cloud_lookup import FoundRoute
    from .run_input import InjectedRun

ROLES = ("source", "target")
CONTEXT_VERSION = 2
TUNNEL_KEYS = tuple(f"{role}-{i}" for role in ROLES for i in range(2))
# Azure가 허용하는 169.254.21.0~22.255 안에서 고른다. OCI가 거부하는 169.254.10.0~19.255와 겹치지 않는다.
# 같은 역할은 어느 클라우드든 같은 주소를 쓴다.
BGP_NETWORKS = {"source": ("169.254.21.0/30", "169.254.22.0/30"),
                "target": ("169.254.21.4/30", "169.254.22.4/30")}


class VpnError(RuntimeError):
    """코드는 호출자 분기용이며 메시지에는 비밀값/도구 응답 원문을 넣지 않는다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VpnLeg:
    role: str
    cloud: str
    region: str
    scope_id: str
    cidrs: tuple[str, ...]
    network: dict
    db_port: int
    credentials_ref: str

    @property
    def keys(self) -> tuple[str, str]:
        return f"{self.role}-0", f"{self.role}-1"

    def inside_cidr(self, index: int) -> str:
        return BGP_NETWORKS[self.role][index]

    def peer_bgp_ip(self, index: int) -> str:
        return str(ipaddress.ip_network(self.inside_cidr(index))[1])

    def oracle_bgp_ip(self, index: int) -> str:
        return str(ipaddress.ip_network(self.inside_cidr(index))[2])


@dataclass(frozen=True)
class VpnPlan:
    run_id: str
    job_id: str
    source: VpnLeg
    target: VpnLeg

    @property
    def name(self) -> str:
        return "cdc-" + self.run_id

    @property
    def legs(self) -> tuple[VpnLeg, VpnLeg]:
        return self.source, self.target

    @classmethod
    def from_route(cls, injected: "InjectedRun", found: "FoundRoute") -> "VpnPlan":
        legs = []
        for role in ROLES:
            endpoint, ref = getattr(found, role), getattr(injected, role)
            if (endpoint.cloud, endpoint.region) != (ref.cloud, ref.region):
                raise VpnError("route_mismatch", "lookup route does not match the injected input")
            legs.append(VpnLeg(role, endpoint.cloud, endpoint.region, endpoint.scope_id, tuple(endpoint.cidrs),
                               copy.deepcopy(endpoint.network), endpoint.db.port, ref.credentials_ref))
        return cls(injected.run_id, injected.run_id.replace("-", "_"), *legs)

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", self.run_id) or self.job_id != self.run_id.replace("-", "_"):
            raise VpnError("invalid_plan", "invalid VPN run or job ID")
        if self.source.role != "source" or self.target.role != "target" or self.source.cloud == self.target.cloud:
            raise VpnError("invalid_plan", "VPN requires source and target in different clouds")
        for leg in self.legs:
            if not leg.region or not leg.scope_id or not leg.credentials_ref.startswith("secret://"):
                raise VpnError("invalid_plan", f"{leg.role}: incomplete VPN input")
            if type(leg.db_port) is not int or not 1 <= leg.db_port <= 65535:
                raise VpnError("invalid_plan", f"{leg.role}: invalid DB port")
            try:
                nets = [ipaddress.ip_network(c, strict=False) for c in leg.cidrs]
            except (ValueError, TypeError):
                raise VpnError("invalid_plan", f"{leg.role}: invalid network ranges") from None
            if not any(n.version == 4 for n in nets):
                raise VpnError("invalid_plan", f"{leg.role}: an IPv4 VPN network is required")

    def context(self) -> dict:
        return {"version": CONTEXT_VERSION, **asdict(self)}

    @classmethod
    def from_context(cls, data: dict) -> "VpnPlan":
        try:
            if data.get("version") != CONTEXT_VERSION:
                raise VpnError("legacy_context", "this run has a legacy VPN; clean it up before using the new VPN flow")
            legs = [VpnLeg(**{**data[role], "cidrs": tuple(data[role]["cidrs"])}) for role in ROLES]
            plan = cls(data["run_id"], data["job_id"], *legs)
            plan.validate()
            return plan
        except (KeyError, TypeError, ValueError, AttributeError):
            raise VpnError("invalid_context", "saved VPN context is invalid") from None


@dataclass(frozen=True)
class PeerTunnel:
    key: str
    public_ip: str
    asn: str
    # 상대 클라우드가 OCI 기본 PFS 그룹을 받지 못할 때만 지정한다. 없으면 OCI 기본값을 쓴다.
    phase_two_dh_group: str | None = None


def public_ips(value: object, expected: int) -> list[str]:
    try:
        if not isinstance(value, list) or len(value) != expected:
            raise ValueError
        if not all(isinstance(ip, str) and ipaddress.IPv4Address(ip).is_global for ip in value):
            raise ValueError
        if len(set(value)) != expected:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise VpnError("invalid_output", "VPN gateway must provide distinct public IPv4 addresses") from None
