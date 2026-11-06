"""Azure Database for PostgreSQL Flexible Server 원본 수집기 (읽기 전용). 오류는 raise 하지 않고 gaps 로 남깁니다.

임시 입력 규약: rds_instance_id="<resource_group>/<server_name>", 구독은 환경변수 ALPHA_AZURE_SUBSCRIPTION_ID.
session 은 azure credential 객체(없으면 DefaultAzureCredential). 테스트는 client_factory 로 가짜 클라이언트를 주입합니다.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from . import csp, refs, ssh_tunnel
from .bastion import _free_port, _last_line, _listening, _posix, _stop
from .csp import CspSource
from .schemas import AssessmentInput, DbInfo, SourceDb, SourceNetwork

SUBSCRIPTION_ENV = "ALPHA_AZURE_SUBSCRIPTION_ID"
BASTION_ENV = "ALPHA_AZURE_BASTION"  # "<resource_group>/<name>"
_ID = re.compile(r"^([\w.()-]{1,90})/([a-z0-9][a-z0-9-]{0,61})$")
_BASTION_ID = re.compile(r"^([\w.()-]{1,90})/([\w][\w.-]{0,79})$")
_VM_ID = re.compile(r"^/subscriptions/[\w-]+/resourceGroups/[\w.()-]+/providers/Microsoft\.Compute/virtualMachines/[\w.-]+$", re.I)


class BastionTunnelError(Exception):
    """az bastion tunnel 실패. 메시지에 비밀값을 넣지 않는다."""


def _client(credential: Any, subscription_id: str):
    from azure.identity import DefaultAzureCredential  # 지연 import: pip install azure-identity azure-mgmt-rdbms
    from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient

    return PostgreSQLManagementClient(credential or DefaultAzureCredential(), subscription_id)


def _compute(credential: Any, subscription_id: str):
    from azure.identity import DefaultAzureCredential  # pip install azure-mgmt-compute
    from azure.mgmt.compute import ComputeManagementClient

    return ComputeManagementClient(credential or DefaultAzureCredential(), subscription_id)


def _network(credential: Any, subscription_id: str):
    from azure.identity import DefaultAzureCredential  # pip install azure-mgmt-network
    from azure.mgmt.network import NetworkManagementClient

    return NetworkManagementClient(credential or DefaultAzureCredential(), subscription_id)


def _cidrs(rules) -> list[str]:
    out = []
    for r in rules or []:
        a, b = getattr(r, "start_ip_address", None), getattr(r, "end_ip_address", None)
        if not a or not b:
            continue
        try:
            out += [str(n) for n in ipaddress.summarize_address_range(ipaddress.ip_address(a), ipaddress.ip_address(b))]
        except ValueError:
            out.append(f"{a}-{b}")
    return out


def _rg_name(rid: str) -> tuple[str, str]:
    p = rid.split("/")
    return p[4], p[-1]


def _vnet(subnet_id: str | None) -> str | None:
    return subnet_id.split("/subnets/")[0].lower() if subnet_id else None


def find_bastion(session: Any, sub: str, subnet_id: str | None, compute_factory, network_factory):
    """(접속 주소, VM 리소스 ID, VNet ID, 실패 사유). 태그 Purpose=alpha-assessment-bastion + running VM, 서버와 같은 VNet 우선, 공용 IP 우선."""
    tag_k, tag_v = ssh_tunnel.BASTION_TAG
    try:
        compute, network = compute_factory(session, sub), network_factory(session, sub)
        vms = [v for v in compute.virtual_machines.list_all() if (getattr(v, "tags", None) or {}).get(tag_k) == tag_v]
        if not vms:
            return None, None, None, "bastion 없음 (태그 VM 미발견)"
        cands = []
        for v in vms:
            rg, n = _rg_name(v.id)
            codes = [st.code for st in compute.virtual_machines.instance_view(rg, n).statuses or []]
            if "PowerState/running" not in codes:
                continue
            for ref in v.network_profile.network_interfaces:
                nic = network.network_interfaces.get(*_rg_name(ref.id))
                for ipc in nic.ip_configurations or []:
                    addr = ipc.private_ip_address
                    if ipc.public_ip_address is not None:
                        addr = network.public_ip_addresses.get(*_rg_name(ipc.public_ip_address.id)).ip_address or addr
                    if addr:
                        same = bool(subnet_id) and _vnet(getattr(ipc.subnet, "id", None)) == _vnet(subnet_id)
                        cands.append((same, addr, v.id, _vnet(getattr(ipc.subnet, "id", None))))
        if not cands:
            return None, None, None, "bastion VM 이 running 아니거나 IP 없음"
        best = max(cands, key=lambda c: c[0])
        return best[1], best[2], best[3], ""
    except Exception as e:
        return None, None, None, f"bastion 조회 실패 ({_err(e)})"


def find_azure_bastion(session: Any, sub: str, vnet: str | None, network_factory) -> tuple[tuple[str, str] | None, str]:
    """((rg, name), 실패 사유). 환경변수 우선, 없으면 같은 VNet 의 Azure Bastion 리소스."""
    env = os.environ.get(BASTION_ENV)
    if env:
        m = _BASTION_ID.match(env)
        return (m.groups(), "") if m else (None, f"{BASTION_ENV} 형식 오류 ('<리소스그룹>/<이름>')")
    try:
        for b in network_factory(session, sub).bastion_hosts.list():
            if vnet and any(_vnet(getattr(getattr(c, "subnet", None), "id", None)) == vnet for c in b.ip_configurations or []):
                return _rg_name(b.id), ""
    except Exception as e:
        return None, f"Azure Bastion 조회 실패 ({_err(e)})"
    return None, "같은 VNet 의 Azure Bastion 리소스 없음"


@contextmanager
def bastion_tunnel(rg: str, name: str, vm_id: str, target_host: str, target_port: int = 5432, *, ready_timeout: float = 60.0):
    """1단 az bastion tunnel(로컬 포트 N) → 2단 ssh -L(N 경유). 나갈 때 ssh → az 순으로 정리."""
    if not _BASTION_ID.match(f"{rg}/{name}") or not _VM_ID.match(vm_id or ""):
        raise BastionTunnelError("Bastion/VM 식별자 형식이 올바르지 않음")
    if not shutil.which("az"):
        raise BastionTunnelError("az 없음: Azure CLI 설치 및 az login 필요")
    port = _free_port()
    cmd = ["az", "network", "bastion", "tunnel", "--name", name, "--resource-group", rg,
           "--target-resource-id", vm_id, "--resource-port", "22", "--port", str(port)]
    proc = subprocess.Popen(cmd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            **({"start_new_session": True} if _posix() else {}))
    try:
        deadline = time.monotonic() + ready_timeout
        while not _listening(port):
            if proc.poll() is not None:
                why = _last_line(proc)
                raise BastionTunnelError("az bastion tunnel 이 먼저 종료됨" + (f": {why}" if why else ""))
            if time.monotonic() >= deadline:
                raise BastionTunnelError(f"az bastion tunnel 준비 시간 초과 ({ready_timeout:g}초)")
            time.sleep(0.2)
        with ssh_tunnel.forward("127.0.0.1", target_host, target_port, ssh_port=port, proxy_command="") as hp:
            yield hp
    finally:
        _stop(proc)


def _err(e: Exception) -> str:
    code = getattr(getattr(e, "error", None), "code", None) or getattr(e, "status_code", None)
    return f"{type(e).__name__} {code}" if code else type(e).__name__


def collect(inp: AssessmentInput, session: Any, client_factory: Callable[[Any, str], Any] = _client,
            compute_factory: Callable[[Any, str], Any] = _compute, network_factory: Callable[[Any, str], Any] = _network) -> CspSource:
    src = CspSource()
    m = _ID.match(inp.rds_instance_id or "")
    if not m:
        src.gaps.append("Azure 서버 식별 형식 오류: rds_instance_id 는 '<리소스그룹>/<서버이름>' 이어야 함")
        return src
    rg, name = m.groups()
    sub = os.environ.get(SUBSCRIPTION_ENV)
    if not sub:
        src.gaps.append(f"Azure 구독이 지정되지 않음: 환경변수 {SUBSCRIPTION_ENV} 필요")
        return src
    try:
        client = client_factory(session, sub)
        s = client.servers.get(rg, name)
    except Exception as e:
        missing = getattr(e, "status_code", None) == 404
        src.gaps.append("Azure PostgreSQL 서버를 찾지 못함" if missing else f"Azure PostgreSQL 서버 조회 실패 ({_err(e)})")
        return src

    net = getattr(s, "network", None)
    public = None if net is None or net.public_network_access is None else net.public_network_access == "Enabled"
    subnet = getattr(net, "delegated_subnet_resource_id", None)
    sku, storage = getattr(s, "sku", None), getattr(s, "storage", None)
    host = getattr(s, "fully_qualified_domain_name", None)
    ha_mode = getattr(getattr(s, "high_availability", None), "mode", None)

    flag = None
    try:
        flag = client.configurations.get(rg, name, csp.LOGICAL_FLAG["azure"]).value
    except Exception as e:
        src.gaps.append(f"논리 복제 파라미터 조회 실패 ({_err(e)})")
    lr = csp.logical_enabled("azure", flag)

    src.db = DbInfo(
        engine="postgres", publicly_accessible=public, engine_version=getattr(s, "version", None),
        instance_class=getattr(sku, "name", None), allocated_storage_gb=getattr(storage, "storage_size_gb", None),
        logical_replication=lr,
        ha_enabled=None if ha_mode is None else ha_mode != "Disabled",
        backup_retention_days=getattr(getattr(s, "backup", None), "backup_retention_days", None),
    )
    src.source_db = SourceDb(
        instance_id=f"{rg}/{name}", host=host, port=5432, version=src.db.engine_version,
        publicly_accessible=public, logical_replication=lr,
    )
    src.network = SourceNetwork(vpc_id=subnet)  # 위임 서브넷 ID (프라이빗 접근일 때)

    rules = None
    if public:
        try:
            rules = list(client.firewall_rules.list_by_server(rg, name))
        except Exception as e:
            src.gaps.append(f"방화벽 규칙 조회 실패 ({_err(e)})")
    src.network.allowed_cidrs = _cidrs(rules)
    direct_ok = bool(public and rules and host)
    if not direct_ok and not public:
        src.gaps.append("서버가 공개 접근이 아니거나 확인 불가 — 중계 VM 경유 시도")
    elif not direct_ok:
        src.gaps.append("공개 접근이지만 방화벽 규칙/FQDN 이 없거나 확인 불가 — 중계 VM 경유 시도")

    def direct_tunnel():
        # ponytail: 방화벽이 이 실행 주소를 허용하는지는 검사하지 않음(접속 실패는 DB 접속 gap 으로 드러남)
        @contextmanager
        def direct():
            yield host, 5432

        return direct

    if direct_ok and public:
        src.tunnel = direct_tunnel()
    elif host:
        addr, vm_id, vnet, why = find_bastion(session, sub, subnet, compute_factory, network_factory)
        bst = None
        if addr:
            if not shutil.which("az"):
                src.gaps.append("az 없음 — Azure Bastion 터널 사용 불가")
            else:
                bst, bwhy = find_azure_bastion(session, sub, vnet or _vnet(subnet), network_factory)
                if bst is None:
                    src.gaps.append(f"{bwhy} — Azure Bastion 터널 사용 불가")
        if bst:
            src.tunnel = lambda: bastion_tunnel(*bst, vm_id, host)
        elif addr:
            src.tunnel = lambda: ssh_tunnel.forward(addr, host, 5432)
        else:
            src.gaps.append(f"{why} — DB 접속을 건너뜀")
    else:
        src.gaps.append("서버 FQDN 을 읽지 못해 DB 접속을 건너뜀")

    if src.tunnel is not None:
        creds, reason = refs.resolve_db_admin(inp.db_admin_ref)
        if creds is None:
            src.gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
            src.tunnel = None
        else:
            src.creds = creds
    return src
