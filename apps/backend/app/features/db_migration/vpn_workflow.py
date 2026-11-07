"""7번 공통 절차: 양쪽 게이트웨이 → OCI → 양쪽 연결 → 상태 확인.

호출: ensure_vpn(VpnPlan.from_route(injected, found)). 5·6번을 통과한 뒤 호출한다.
부분 실패 시 context와 PSK를 보존한다. destroy는 구간별로 계속 시도하며 다시 호출할 수 있다.
"""

import ipaddress
import json
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from app.core.secrets import put_secret, resolve

from .progress_log import ProgressLog
from .settings import tunables
from .vpn import OciSettings
from .vpn_peers import PeerAdapter, TerraformPort, adapters_for
from .vpn_plan import ROLES, TUNNEL_KEYS, PeerTunnel, VpnError, VpnPlan, public_ips
from .vpn_terraform import TerraformSession

_busy: set[str] = set()
_lock = threading.Lock()


@contextmanager
def _operation(run_id: str):
    # Terraform가 아닌 단계(context/PSK 저장)도 같은 run끼리 겹치면 안 된다.
    with _lock:
        if run_id in _busy:
            raise VpnError("busy", "VPN operation is already running for this run")
        _busy.add(run_id)
    try:
        yield
    finally:
        with _lock:
            _busy.discard(run_id)


def _load(run_id: str) -> VpnPlan | None:
    try:
        raw = resolve(f"secret://{run_id}/vpn-context")
    except KeyError:
        return None
    try:
        plan = VpnPlan.from_context(json.loads(raw["json"]))
        if plan.run_id != run_id:
            raise ValueError
        return plan
    except (ValueError, KeyError, TypeError):
        raise VpnError("invalid_context", "saved VPN context is invalid") from None


def _psk(run_id: str, *, create: bool) -> dict[str, str]:
    ref = f"secret://{run_id}/vpn-psk"
    try:
        values = resolve(ref)
    except KeyError:
        if not create:
            raise VpnError("missing_psk", "saved VPN secrets are missing; existing tunnel keys must not be regenerated") from None
        values = {key: secrets.token_hex(24) for key in TUNNEL_KEYS}
        put_secret(ref, values)
    if set(values) != set(TUNNEL_KEYS) or not all(
        isinstance(v, str) and len(v) == 48 and all(c in "0123456789abcdef" for c in v) for v in values.values()
    ):
        raise VpnError("invalid_psk", "saved VPN secrets have an incompatible format")
    return values


def _session(plan: VpnPlan, adapters: dict[str, PeerAdapter], settings: OciSettings, *, check: bool) -> TerraformSession:
    roots = {leg.role: adapters[leg.role].root for leg in plan.legs}
    roots["oci"] = "oci-vpn"
    credentials = {leg.role: (lambda leg=leg: adapters[leg.role].credentials(resolve(leg.credentials_ref))) for leg in plan.legs}
    credentials["oci"] = lambda: {}
    tf = TerraformSession(settings, plan.job_id, roots, credentials, log=ProgressLog(plan.run_id, plan.job_id))
    if check:
        tf.check_credentials()
    return tf


def _oracle_ips(tf: TerraformPort) -> dict:
    values = tf.output("oci", "oracle_tunnel_ips")
    if values is None or values == {}:
        return {}
    if not isinstance(values, dict) or set(values) != set(TUNNEL_KEYS):
        raise VpnError("invalid_output", "OCI returned an incomplete set of VPN gateway addresses")
    for role in ROLES:
        public_ips([values[f"{role}-0"], values[f"{role}-1"]], 2)
    return values


def _oci_variables(plan: VpnPlan, settings: OciSettings, psk: dict, peers: list[PeerTunnel]) -> dict:
    by_key = {peer.key: peer for peer in peers}
    if len(peers) != 4 or set(by_key) != set(TUNNEL_KEYS):
        raise VpnError("invalid_output", "VPN peers did not provide the required four tunnels")
    tunnels = {}
    for leg in plan.legs:
        cidrs = [str(ipaddress.ip_network(c, strict=False)) for c in leg.cidrs if ipaddress.ip_network(c, strict=False).version == 4]
        for i, key in enumerate(leg.keys):
            peer = by_key[key]
            tunnels[key] = {"cpe_ip": peer.public_ip, "peer_asn": peer.asn, "peer_cidrs": cidrs,
                            "oracle_bgp_ip": f"{leg.oracle_bgp_ip(i)}/30", "peer_bgp_ip": f"{leg.peer_bgp_ip(i)}/30"}
            if peer.phase_two_dh_group:
                tunnels[key]["phase_two_dh_group"] = peer.phase_two_dh_group
    return {"region": settings.region, "config_profile": settings.profile, "compartment_id": settings.compartment_id,
            "name": plan.name, "drg_id": settings.drg_id, "tunnels": tunnels, "shared_secrets": psk}


def _up(tf: TerraformPort, values: dict, log: ProgressLog) -> dict | None:
    tf.refresh("oci", values)
    status = tf.output("oci", "tunnel_status")
    log.event("vpn.tunnels", status=status)
    if isinstance(status, dict) and set(status) == set(TUNNEL_KEYS) and all(v == "UP" for v in status.values()):
        return status
    return None


def ensure_vpn(plan: VpnPlan, *, terraform: TerraformPort | None = None, settings: OciSettings | None = None,
               registry: dict[str, PeerAdapter] | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    plan.validate()
    adapters = adapters_for(plan.legs, registry)
    log = ProgressLog(plan.run_id, plan.job_id)
    with _operation(plan.run_id):
        saved = _load(plan.run_id)
        if saved is not None and saved != plan:
            raise VpnError("context_changed", "VPN input changed; clean up the saved VPN before changing this run")
        settings = settings or OciSettings.from_env()
        tf = terraform or _session(plan, adapters, settings, check=True)
        psk = _psk(plan.run_id, create=saved is None)
        put_secret(f"{plan.run_id}/vpn-context", {"json": json.dumps(plan.context())})
        oci_ips = _oracle_ips(tf)
        if saved and oci_ips:
            peers = [peer for leg in plan.legs for peer in adapters[leg.role].peers(tf, leg)]
            if len(peers) == 4:
                values = _oci_variables(plan, settings, psk, peers)
                if status := _up(tf, values, log):
                    return {"name": plan.name, "tunnel_status": status, "skipped": True}

        # 어느 클라우드가 source인지에 따라 순서를 바꾸지 않는다.
        peers = []
        for leg in plan.legs:
            adapter = adapters[leg.role]
            tf.apply(leg.role, adapter.variables(leg, plan.name, settings, psk, oci_ips))
            peers.extend(adapter.peers(tf, leg))
        values = _oci_variables(plan, settings, psk, peers)
        tf.apply("oci", values)
        oci_ips = _oracle_ips(tf)
        if not oci_ips:
            raise VpnError("invalid_output", "OCI did not provide VPN gateway addresses after apply")
        for leg in plan.legs:
            adapter = adapters[leg.role]
            tf.apply(leg.role, adapter.variables(leg, plan.name, settings, psk, oci_ips))
            expected = [peer for peer in peers if peer.key in leg.keys]
            if adapter.peers(tf, leg) != expected:
                raise VpnError("peer_changed", f"{leg.role}: gateway addresses or BGP settings changed while connecting OCI")
        operating = tunables()
        deadline = time.monotonic() + operating.tunnel_wait_s
        while True:
            if status := _up(tf, values, log):
                return {"name": plan.name, "tunnel_status": status, "skipped": False}
            if time.monotonic() >= deadline:
                raise VpnError("tunnel_timeout", "VPN tunnels did not become ready; keep the saved context for retry or cleanup")
            sleep(operating.tunnel_poll_s)


def destroy_vpn(run_id: str, *, terraform: TerraformPort | None = None, settings: OciSettings | None = None,
                registry: dict[str, PeerAdapter] | None = None) -> bool:
    with _operation(run_id):
        plan = _load(run_id)
        if plan is None:
            return False
        adapters = adapters_for(plan.legs, registry)
        settings = settings or OciSettings.from_env()
        tf = terraform or _session(plan, adapters, settings, check=False)
        psk = _psk(run_id, create=False)
        errors = []
        # 부분 구축으로 출력이 없어도 지울 수 있도록 destroy 입력에만 임시값을 넣는다.
        # apply/refresh는 호출하지 않는다. 삭제 대상은 저장된 state의 실제 자원이다.
        placeholders = {key: f"1.1.1.{i + 1}" for i, key in enumerate(TUNNEL_KEYS)}
        peers = [PeerTunnel(key, placeholders[key], adapters[leg.role].asn) for leg in plan.legs for key in leg.keys]
        for leg in reversed(plan.legs):
            try:
                tf.destroy(leg.role, adapters[leg.role].variables(leg, plan.name, settings, psk, placeholders))
            except Exception:
                errors.append(leg.role)
        try:
            tf.destroy("oci", _oci_variables(plan, settings, psk, peers))
        except Exception:
            errors.append("oci")
        if errors:
            raise VpnError("cleanup_failed", f"VPN cleanup failed in: {', '.join(errors)}; saved context is kept for retry")
        # context/PSK 삭제는 기존 stop/finish가 다른 실행 비밀값과 함께 맡는다.
        return True
