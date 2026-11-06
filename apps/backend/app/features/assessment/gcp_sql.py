"""GCP Cloud SQL for PostgreSQL 원본 수집기 (읽기 전용). 실패는 raise 하지 않고 gaps 로, 오류는 상태코드/클래스명만."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any

from . import refs, ssh_tunnel
from .bastion import _posix, _stop
from .csp import LOGICAL_FLAG, CspSource, logical_enabled
from .schemas import AssessmentInput, DbInfo, SourceDb, SourceNetwork

PROJECT_ENV = "ALPHA_GCP_PROJECT"
PROXY_BIN = "cloud-sql-proxy"
PROXY_ENV = "ALPHA_BASTION_PROXY_COMMAND_GCP"
_SAFE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BASTION_LABEL = (ssh_tunnel.BASTION_TAG[0].lower(), ssh_tunnel.BASTION_TAG[1].lower())


def _compute(session: Any):
    from googleapiclient.discovery import build

    return build("compute", "v1", credentials=session, cache_discovery=False)


def _net(url: str | None) -> str:
    return (url or "").partition("projects/")[2]


def find_bastion(compute: Any, project: str, vpc: str | None) -> dict | None:
    """라벨 purpose=alpha-assessment-bastion 인 RUNNING VM 의 {name, zone, project}. 같은 VPC 우선."""
    k, v = BASTION_LABEL
    found, token = [], None
    while True:
        r = compute.instances().aggregatedList(
            project=project, filter=f"labels.{k}={v} AND status=RUNNING", pageToken=token).execute()
        for scoped in (r.get("items") or {}).values():
            for i in scoped.get("instances") or []:
                if (i.get("labels") or {}).get(k) == v and i.get("status") == "RUNNING" and i.get("networkInterfaces"):
                    found.append(i)
        token = r.get("nextPageToken")
        if not token:
            break

    def rank(i):
        n = i["networkInterfaces"][0]
        return _net(n.get("network")) != _net(vpc) if vpc else 0, -(i.get("id") and int(i["id"]) or 0)

    for i in sorted(found, key=rank):
        n = i["networkInterfaces"][0]
        if i.get("name"):
            return {"name": i["name"], "zone": (i.get("zone") or "").rsplit("/", 1)[-1], "project": project}
    return None


def iap_command(b: dict) -> str:
    if not all(_SAFE.match(b.get(k) or "") for k in ("name", "zone", "project")):
        raise ValueError("중계 VM 이름/zone/project 형식이 안전하지 않음")
    return (f"gcloud compute start-iap-tunnel {b['name']} 22 --listen-on-stdin "
            f"--project {b['project']} --zone {b['zone']}")


def ssh_tunnel_factory(bastion: str, host: str, proxy_command: str | None = None):
    return lambda: ssh_tunnel.forward(bastion, host, 5432, proxy_command=proxy_command)


def _client(session: Any):
    from googleapiclient.discovery import build  # 지연 import (google-api-python-client)

    return build("sqladmin", "v1", credentials=session, cache_discovery=False)


def _err(e: Exception) -> str:
    resp = getattr(e, "resp", None)
    return f"{type(e).__name__} {getattr(resp, 'status', '')}".strip()


def proxy_tunnel(connection_name: str, private_ip: bool, timeout: float = 20.0):
    """cloud-sql-proxy 를 띄워 (127.0.0.1, port) 를 준다. 실환경 미검증."""

    @contextlib.contextmanager
    def _open():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not shutil.which(PROXY_BIN):
            raise RuntimeError("cloud-sql-proxy 바이너리 없음: 설치하거나 중계 VM 을 준비하세요")
        cmd = [PROXY_BIN, "--address", "127.0.0.1", "--port", str(port)] + (["--private-ip"] if private_ip else []) + [connection_name]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **({"start_new_session": True} if _posix() else {}))
        try:
            end = time.time() + timeout
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), 1).close()
                    break
                except OSError:
                    if proc.poll() is not None or time.time() > end:
                        raise RuntimeError("cloud-sql-proxy 시작 실패")
                    time.sleep(0.5)
            yield "127.0.0.1", port
        finally:
            _stop(proc)

    return _open


def collect(
    inp: AssessmentInput,
    session: Any,
    client_factory: Callable[[Any], Any] = _client,
    tunnel_factory: Callable[[str, bool], Callable] = proxy_tunnel,
    compute_factory: Callable[[Any], Any] = _compute,
    ssh_factory: Callable[..., Callable] = ssh_tunnel_factory,
) -> CspSource:
    src = CspSource()
    project = os.environ.get(PROJECT_ENV)
    if not project:
        src.gaps.append(f"GCP 프로젝트를 알 수 없음 ({PROJECT_ENV} 미설정)")
        return src
    if not inp.rds_instance_id:
        src.gaps.append("Cloud SQL 인스턴스 이름(rds_instance_id)이 지정되지 않음")
        return src
    try:
        d = client_factory(session).instances().get(project=project, instance=inp.rds_instance_id).execute()
    except Exception as e:
        src.gaps.append(f"Cloud SQL 조회 실패 ({_err(e)})")
        return src

    ver = d.get("databaseVersion") or ""
    if not ver.startswith("POSTGRES"):
        src.gaps.append("Cloud SQL 인스턴스가 PostgreSQL 이 아니거나 엔진을 확인하지 못함")
    st = d.get("settings") or {}
    ip = st.get("ipConfiguration") or {}
    flags = {f.get("name"): f.get("value") for f in st.get("databaseFlags") or []}
    # settings 를 읽었으면 플래그 미설정 = 기본값 off
    lr = logical_enabled("gcp", flags.get(LOGICAL_FLAG["gcp"], "off")) if st else None
    if not st:
        src.gaps.append("Cloud SQL settings 를 읽지 못해 논리 복제 설정을 확인하지 못함")
    version = ver.split("_", 1)[1] if ver.startswith("POSTGRES_") else None
    disk = st.get("dataDiskSizeGb")
    try:
        disk_gb = int(disk) if disk else None
    except (TypeError, ValueError):
        disk_gb = None
        src.gaps.append("디스크 크기 값을 해석하지 못함")
    public = ip.get("ipv4Enabled")
    addrs = {a.get("type"): a.get("ipAddress") for a in d.get("ipAddresses") or []}
    host = addrs.get("PRIVATE") or addrs.get("PRIMARY")
    src.db = DbInfo(
        engine="postgres" if ver.startswith("POSTGRES") else None, publicly_accessible=public, engine_version=version,
        instance_class=st.get("tier"), allocated_storage_gb=disk_gb, logical_replication=lr,
        ha_enabled=None if "availabilityType" not in st else st["availabilityType"] == "REGIONAL",
        backup_retention_days=((st.get("backupConfiguration") or {}).get("backupRetentionSettings") or {}).get("retainedBackups"))
    src.source_db = SourceDb(
        instance_id=d.get("name"), host=host, port=5432 if host else None, version=version,
        publicly_accessible=public, logical_replication=lr)
    src.network = SourceNetwork(
        vpc_id=ip.get("privateNetwork"), allowed_cidrs=[n["value"] for n in ip.get("authorizedNetworks") or [] if n.get("value")])
    conn = d.get("connectionName")

    src.creds, reason = refs.resolve_db_admin(inp.db_admin_ref)
    if src.creds is None:
        src.gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
    bastion = None
    if host:
        try:
            bastion = find_bastion(compute_factory(session), project, ip.get("privateNetwork"))
        except Exception as e:
            src.gaps.append(f"중계 VM 조회 실패 ({_err(e)})")
        if bastion is None and not any("중계 VM 조회" in g for g in src.gaps):
            src.gaps.append("라벨 purpose=alpha-assessment-bastion 인 RUNNING 중계 VM 을 찾지 못함")
    cmd = None
    if bastion:
        try:
            cmd = os.environ.get(PROXY_ENV) or (iap_command(bastion) if shutil.which("gcloud") else None)
            if cmd is None:
                src.gaps.append("`gcloud` 없음(IAP 불가)")
        except ValueError as e:
            src.gaps.append(str(e))
    if cmd:
        src.tunnel = ssh_factory(bastion["name"], host, cmd)
    elif not conn:
        src.gaps.append("connectionName 을 읽지 못해 접속 경로를 만들 수 없음")
    elif tunnel_factory is proxy_tunnel and not shutil.which(PROXY_BIN):
        src.gaps.append("접속 경로 없음: 중계 VM 도 cloud-sql-proxy 바이너리도 없음")
    else:
        src.tunnel = tunnel_factory(conn, bool(addrs.get("PRIVATE")) and not public)
    return src
