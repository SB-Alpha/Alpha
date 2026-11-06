"""Azure Database for PostgreSQL Flexible Server 원본 수집기 (읽기 전용). 오류는 raise 하지 않고 gaps 로 남깁니다.

임시 입력 규약: rds_instance_id="<resource_group>/<server_name>", 구독은 환경변수 ALPHA_AZURE_SUBSCRIPTION_ID.
session 은 azure credential 객체(없으면 DefaultAzureCredential). 테스트는 client_factory 로 가짜 클라이언트를 주입합니다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from . import csp, refs
from .csp import CspSource
from .schemas import AssessmentInput, DbInfo, SourceDb, SourceNetwork

SUBSCRIPTION_ENV = "ALPHA_AZURE_SUBSCRIPTION_ID"
_ID = re.compile(r"^([\w.()-]{1,90})/([a-z0-9][a-z0-9-]{0,61})$")


def _client(credential: Any, subscription_id: str):
    from azure.identity import DefaultAzureCredential  # 지연 import: pip install azure-identity azure-mgmt-rdbms
    from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient

    return PostgreSQLManagementClient(credential or DefaultAzureCredential(), subscription_id)


def _err(e: Exception) -> str:
    code = getattr(getattr(e, "error", None), "code", None) or getattr(e, "status_code", None)
    return f"{type(e).__name__} {code}" if code else type(e).__name__


def collect(inp: AssessmentInput, session: Any, client_factory: Callable[[Any, str], Any] = _client) -> CspSource:
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
    if not public:
        src.gaps.append("서버가 공개 접근이 아니거나 확인 불가 — Bastion/프라이빗 경로는 아직 구현되지 않아 DB 접속을 건너뜀")
    elif not rules:
        src.gaps.append("공개 접근이지만 방화벽 규칙이 없거나 확인 불가 — DB 접속을 건너뜀")
    elif not host:
        src.gaps.append("서버 FQDN 을 읽지 못해 DB 접속을 건너뜀")
    else:
        # ponytail: 방화벽이 이 실행 주소를 허용하는지는 검사하지 않음(접속 실패는 DB 접속 gap 으로 드러남)
        @contextmanager
        def direct():
            yield host, 5432

        src.tunnel = direct

    if src.tunnel is not None:
        creds, reason = refs.resolve_db_admin(inp.db_admin_ref)
        if creds is None:
            src.gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
            src.tunnel = None
        else:
            src.creds = creds
    return src
