"""GCP Cloud SQL for PostgreSQL 원본 수집기 (읽기 전용). 실패는 raise 하지 않고 gaps 로, 오류는 상태코드/클래스명만."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any

from . import refs
from .csp import LOGICAL_FLAG, CspSource, logical_enabled
from .schemas import AssessmentInput, DbInfo, SourceDb, SourceNetwork

PROJECT_ENV = "ALPHA_GCP_PROJECT"
PROXY_BIN = "cloud-sql-proxy"


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
        cmd = [PROXY_BIN, "--address", "127.0.0.1", "--port", str(port)] + (["--private-ip"] if private_ip else []) + [connection_name]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            proc.terminate()

    return _open


def collect(
    inp: AssessmentInput,
    session: Any,
    client_factory: Callable[[Any], Any] = _client,
    tunnel_factory: Callable[[str, bool], Callable] = proxy_tunnel,
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
    public = ip.get("ipv4Enabled")
    addrs = {a.get("type"): a.get("ipAddress") for a in d.get("ipAddresses") or []}
    host = addrs.get("PRIVATE") or addrs.get("PRIMARY")
    src.db = DbInfo(
        engine="postgres" if ver.startswith("POSTGRES") else None, publicly_accessible=public, engine_version=version,
        instance_class=st.get("tier"), allocated_storage_gb=int(disk) if disk else None, logical_replication=lr)
    src.source_db = SourceDb(
        instance_id=d.get("name"), host=host, port=5432 if host else None, version=version,
        publicly_accessible=public, logical_replication=lr)
    src.network = SourceNetwork(vpc_id=ip.get("privateNetwork"))
    conn = d.get("connectionName")

    src.creds, reason = refs.resolve_db_admin(inp.db_admin_ref)
    if src.creds is None:
        src.gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
    if not conn:
        src.gaps.append("connectionName 을 읽지 못해 접속 경로를 만들 수 없음")
    else:
        src.tunnel = tunnel_factory(conn, bool(addrs.get("PRIVATE")) and not public)
    return src
