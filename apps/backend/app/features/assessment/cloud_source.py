"""GCP·Azure 원본 자동 조회 (source_kind="gcp" / "azure"). (이주호)

AWS 조회(db.py·app.py)와 같은 형식(DbAssessment · AppAssessment)을 만듭니다. 새 의존성 없이 REST 로 읽습니다.

- GCP: Cloud Run 서비스(run.googleapis.com v2) + Cloud SQL 인스턴스(sqladmin v1). 토큰은 서비스 계정 키 이름표, 없으면 ADC.
- Azure: Container Apps + PostgreSQL Flexible Server (management.azure.com). 토큰은 서비스 주체(client credentials).
- 읽기 전용 GET 만 부릅니다. 비밀 값은 읽지 않고, 비밀 참조는 이름만 남깁니다.
- 모르는 값은 지어내지 않고 None 으로 두고 gaps 에 이유를 남깁니다. 예외 메시지에는 값이 섞일 수 있어 클래스명만 씁니다.
- 테이블 목록은 DB 에 직접 붙을 수 있을 때만 읽습니다(공개 주소 등). 못 붙으면 gaps 에 남아 승인 불가가 됩니다.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote
from collections.abc import Callable
from typing import Any

import httpx

from . import pg_connect, pg_tables, refs
from .app import PROVISIONING_INJECTED_ENV, has_secret_value, looks_secret
from .schemas import (
    AppAssessment,
    AppInfo,
    AssessmentInput,
    CloudSource,
    DbAssessment,
    DbInfo,
    SourceDb,
    SourceNetwork,
)

HTTP_TIMEOUT_S = 15
FARGATE_UNITS_PER_VCPU = 1024  # AppInfo.cpu_units 단위에 맞춤
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
AZURE_ARM = "https://management.azure.com"
AZURE_LOGIN = "https://login.microsoftonline.com"
AZURE_APPS_API = "2024-03-01"
AZURE_PG_API = "2022-12-01"
# 사용자 DB 가 아닌 관리용 DB. 앱 DB 이름을 고를 때 뺍니다
SYSTEM_DATABASES = {"postgres", "cloudsqladmin", "azure_maintenance", "azure_sys", "template0", "template1"}

GetJson = Callable[[str, dict[str, str]], dict]


def _seg(value: str | None) -> str:
    """주소 경로 한 칸. 스키마 검증을 거친 값이지만 직접 부를 때를 위해 한 번 더 이스케이프합니다."""
    return quote(value or "", safe="")


def _get_json(url: str, headers: dict[str, str]) -> dict:
    res = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_S)
    res.raise_for_status()
    return res.json()


def _secret(ref: str | None) -> dict | None:
    """secret:// 이름표 → dict. 비어 있으면 None. 값이 있는데 이름표 형식이 아니면 거부합니다 (조용히 다른 로그인으로 넘어가지 않게)."""
    if not ref:
        return None
    if not ref.startswith("secret://"):
        raise ValueError("자격 증명 이름표가 secret://… 형식이 아님")
    from app.core.secrets import resolve  # 지연 import

    data = resolve(ref)
    return json.loads(data) if isinstance(data, str) else data


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------


def gcp_token(credentials_ref: str | None) -> str:
    import google.auth
    import google.auth.transport.urllib3
    import urllib3

    info = _secret(credentials_ref)
    if info:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(info, scopes=[GCP_SCOPE])
    else:
        creds, _ = google.auth.default(scopes=[GCP_SCOPE])
    creds.refresh(google.auth.transport.urllib3.Request(urllib3.PoolManager()))
    return creds.token


def azure_token(credentials_ref: str | None) -> str:
    info = _secret(credentials_ref)
    if not info:
        raise ValueError("Azure 서비스 주체 이름표가 없음")
    missing = [k for k in ("tenant_id", "client_id", "client_secret") if not info.get(k)]
    if missing:
        raise ValueError(f"Azure 서비스 주체 이름표에 키가 없음: {missing}")
    res = httpx.post(
        f"{AZURE_LOGIN}/{info['tenant_id']}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": info["client_id"],
              "client_secret": info["client_secret"], "scope": f"{AZURE_ARM}/.default"},
        timeout=HTTP_TIMEOUT_S,
    )
    res.raise_for_status()
    return res.json()["access_token"]


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def _memory_mib(value: Any) -> int | None:
    """'512Mi' · '1Gi' · '1.5Gi' · '2G' → MiB. 모르는 형식은 None."""
    m = re.fullmatch(r"\s*([\d.]+)\s*(Mi|Gi|M|G)?\s*", str(value or ""))
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "Mi"
    return round(n * (1024 if unit.startswith("G") else 1))


def _cpu_units(value: Any) -> int | None:
    """'1' · '2' · '1000m' · 0.5 → Fargate 단위(1024 = 1 vCPU)."""
    text = str(value if value is not None else "").strip()
    m = re.fullmatch(r"([\d.]+)(m?)", text)
    if not m:
        return None
    vcpu = float(m.group(1)) / (1000 if m.group(2) else 1)
    return round(vcpu * FARGATE_UNITS_PER_VCPU) if vcpu > 0 else None


def _keep_env(name: str, value: str, gaps: list[str]) -> bool:
    """비밀처럼 보이는 평문 값은 넘기지 않습니다 (app.py 와 같은 규칙)."""
    if looks_secret(name) or has_secret_value(value):
        if not (looks_secret(name) and name in PROVISIONING_INJECTED_ENV):
            gaps.append(f"환경 변수 {name} 가 비밀로 보여 값을 넘기지 않음 (비밀 저장소로 옮겨야 함)")
        return False
    return True


def _pick_database(names: list[str], hint: str | None, gaps: list[str]) -> str | None:
    users = [n for n in names if n not in SYSTEM_DATABASES]
    if hint:
        if hint in names:
            return hint
        gaps.append(f"앱이 쓰는 DB({hint})를 원본에서 찾지 못함")
        return None
    if len(users) == 1:
        return users[0]
    if users:
        gaps.append(f"앱 DB 이름을 확정하지 못함 (후보: {', '.join(users)})")
    else:
        gaps.append("앱 DB 를 찾지 못함 (관리용 DB 만 있음)")
    return None


def _read_tables(inp: AssessmentInput, host: str | None, port: int, database: str | None,
                 db: DbInfo, gaps: list[str]) -> DbInfo:
    """DB 에 직접 붙어 테이블·복제 설정을 읽습니다. 못 붙으면 이유만 남깁니다."""
    if not host or not database:
        gaps.append("DB 주소나 이름을 몰라 테이블을 읽지 않음")
        return db
    creds, reason = refs.resolve_db_admin(inp.db_admin_ref)
    if creds is None:
        gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
        return db
    try:
        catalog = pg_connect.collect_catalog(host, port, creds["user"], creds["password"], database)
    except Exception as e:  # 메시지에 비밀번호가 섞일 수 있어 클래스명만
        gaps.append(f"DB 접속 실패 ({type(e).__name__}): 사설 주소만 있으면 같은 네트워크에서 다시 실행해야 함")
        return db
    res = pg_tables.catalog_to_dbinfo(catalog, database)
    gaps += res.gaps
    update: dict[str, Any] = {"wal_level": res.wal_level, "max_replication_slots": res.max_replication_slots,
                              "replication_slots_in_use": res.replication_slots_in_use}
    if res.tables is not None:
        update["tables"] = res.tables
    return db.model_copy(update=update)


def _required(src: CloudSource | None, kind: str) -> CloudSource:
    if src is None:  # AssessmentInput 검증에서 막히지만 직접 부를 때를 위해
        raise ValueError(f"source_kind={kind} 에는 cloud_source 가 필요합니다")
    return src


# ---------------------------------------------------------------------------
# GCP: Cloud Run + Cloud SQL
# ---------------------------------------------------------------------------


def _gcp_app(svc: dict, name: str, gaps: list[str]) -> tuple[AppInfo, str | None, str | None]:
    """(AppInfo, PGUSER, PGDATABASE)."""
    template = svc.get("template") or {}
    containers = template.get("containers") or []
    if not containers:
        gaps.append("Cloud Run 서비스에 컨테이너가 없음")
        return AppInfo(service_name=name, cloud_run_blockers=[]), None, None
    notes = [f"함께 도는 컨테이너 {len(containers) - 1}개 (사이드카 확인 필요)"] if len(containers) > 1 else []
    c = next((x for x in containers if x.get("ports")), containers[0])
    env_vars: dict[str, str] = {}
    secret_refs: dict[str, str] = {}
    for e in c.get("env") or []:
        key = e.get("name")
        if not key:
            continue
        ref = ((e.get("valueSource") or {}).get("secretKeyRef") or {})
        if ref.get("secret"):
            secret_refs[key] = f"{ref['secret']}:{ref.get('version', 'latest')}"  # 이름만. 값은 읽지 않음
        elif _keep_env(key, str(e.get("value", "")), gaps):
            env_vars[key] = str(e.get("value", ""))
    probe = (c.get("startupProbe") or {}).get("httpGet") or (c.get("livenessProbe") or {}).get("httpGet") or {}
    limits = (c.get("resources") or {}).get("limits") or {}
    ports = c.get("ports") or []
    if c.get("command") or c.get("args"):
        notes.append("시작 명령 재정의 있음 (command/args)")
    if template.get("volumes"):
        notes.append(f"볼륨 {len(template['volumes'])}개 (목적지에서 다시 연결 필요)")
    info = AppInfo(
        service_name=name,
        image_uri=c.get("image"),
        container_port=ports[0].get("containerPort") if ports else 8080,  # Cloud Run 기본 포트
        cpu_units=_cpu_units(limits.get("cpu")),
        memory_mib=_memory_mib(limits.get("memory")),
        health_check_path=probe.get("path"),
        env_vars=env_vars,
        secret_refs=secret_refs,
        cloud_run_blockers=[],  # 원본이 Cloud Run 이라 그대로 돕니다
        cloud_run_notes=notes,
    )
    return info, env_vars.get("PGUSER"), env_vars.get("PGDATABASE")


def _gcp_sql_size(tier: str | None) -> tuple[int | None, float | None]:
    """db-custom-<vCPU>-<MiB> 만 계산합니다. 공유 코어 등 다른 등급은 지어내지 않고 None."""
    m = re.fullmatch(r"db-custom-(\d+)-(\d+)", tier or "")
    return (int(m.group(1)), round(int(m.group(2)) / 1024, 2)) if m else (None, None)


def collect_gcp(inp: AssessmentInput, get_json: GetJson | None = None) -> tuple[DbAssessment, AppAssessment, str]:
    src = _required(inp.cloud_source, "gcp")
    app_gaps: list[str] = []
    db_gaps: list[str] = []
    get_json = get_json or _get_json
    token = gcp_token(src.credentials_ref)
    h = {"Authorization": f"Bearer {token}"}
    p, r = src.project_id, src.region

    app_info, pg_user, pg_db = AppInfo(service_name=src.app_name), None, None
    try:
        svc = get_json(f"https://run.googleapis.com/v2/projects/{_seg(p)}/locations/{_seg(r)}/services/{_seg(src.app_name)}", h)
        app_info, pg_user, pg_db = _gcp_app(svc, src.app_name, app_gaps)
    except Exception as e:
        app_gaps.append(f"Cloud Run 서비스 조회 실패 ({type(e).__name__})")

    db = DbInfo(engine="postgres")
    source_db = SourceDb(instance_id=src.db_instance)
    network = SourceNetwork()
    try:
        inst = get_json(f"https://sqladmin.googleapis.com/v1/projects/{_seg(p)}/instances/{_seg(src.db_instance)}", h)
        version = inst.get("databaseVersion") or ""
        settings = inst.get("settings") or {}
        ip_cfg = settings.get("ipConfiguration") or {}
        flags = {f.get("name"): f.get("value") for f in settings.get("databaseFlags") or []}
        addrs = {a.get("type"): a.get("ipAddress") for a in inst.get("ipAddresses") or []}
        public = bool(ip_cfg.get("ipv4Enabled"))
        host = addrs.get("PRIMARY") if public else addrs.get("PRIVATE")
        vcpu, mem = _gcp_sql_size(settings.get("tier"))
        if not version.startswith("POSTGRES"):
            db_gaps.append(f"PostgreSQL 이 아님 ({version or '버전 미확인'})")
        logical = flags.get("cloudsql.logical_decoding") == "on"
        db = DbInfo(
            engine="postgres" if version.startswith("POSTGRES") else (version.lower() or None),
            publicly_accessible=public,
            engine_version=version.removeprefix("POSTGRES_").replace("_", ".") or None,
            username=pg_user,
            instance_class=settings.get("tier"),
            allocated_storage_gb=int(settings["dataDiskSizeGb"]) if settings.get("dataDiskSizeGb") else None,
            logical_replication=logical,
            memory_gib=mem,
            vcpu=vcpu,
        )
        dbs = get_json(f"https://sqladmin.googleapis.com/v1/projects/{_seg(p)}/instances/{_seg(src.db_instance)}/databases", h)
        database = _pick_database([d.get("name") for d in dbs.get("items") or []], pg_db, db_gaps)
        db = db.model_copy(update={"database_name": database})
        source_db = SourceDb(instance_id=src.db_instance, host=host, port=5432, database=database,
                             version=db.engine_version, publicly_accessible=public, logical_replication=logical)
        network = SourceNetwork(vpc_id=ip_cfg.get("privateNetwork"))
        db = _read_tables(inp, host, 5432, database, db, db_gaps)
    except Exception as e:
        db_gaps.append(f"Cloud SQL 조회 실패 ({type(e).__name__})")

    return (DbAssessment(facts=db, source_db=source_db, network=network, gaps=db_gaps),
            AppAssessment(facts=app_info, account_id=p, db_user=pg_user, gaps=app_gaps), r)


# ---------------------------------------------------------------------------
# Azure: Container Apps + PostgreSQL Flexible Server
# ---------------------------------------------------------------------------


def _azure_app(app: dict, name: str, gaps: list[str]) -> tuple[AppInfo, str | None, str | None]:
    props = app.get("properties") or {}
    template = props.get("template") or {}
    ingress = (props.get("configuration") or {}).get("ingress") or {}
    containers = template.get("containers") or []
    if not containers:
        gaps.append("Container App 에 컨테이너가 없음")
        return AppInfo(service_name=name, cloud_run_blockers=[]), None, None
    c = containers[0]
    blockers: list[str] = []
    notes: list[str] = []
    if len(containers) > 1:
        notes.append(f"함께 도는 컨테이너 {len(containers) - 1}개 (사이드카 확인 필요)")
    if (ingress.get("transport") or "").lower() == "tcp":
        blockers.append("TCP 인그레스 (Cloud Run 은 HTTP 만 받음)")
    if not ingress:
        blockers.append("인그레스 없음 (내부 작업 앱은 Cloud Run 서비스로 옮길 수 없음)")
    if template.get("volumes"):
        notes.append(f"볼륨 {len(template['volumes'])}개 (목적지에서 다시 연결 필요)")
    if c.get("command") or c.get("args"):
        notes.append("시작 명령 재정의 있음 (command/args)")
    env_vars: dict[str, str] = {}
    secret_refs: dict[str, str] = {}
    for e in c.get("env") or []:
        key = e.get("name")
        if not key:
            continue
        if e.get("secretRef"):
            secret_refs[key] = f"containerapp-secret:{e['secretRef']}"  # 이름만. 값은 읽지 않음
        elif _keep_env(key, str(e.get("value", "")), gaps):
            env_vars[key] = str(e.get("value", ""))
    probe = next((pr.get("httpGet") for pr in c.get("probes") or []
                  if pr.get("type") in ("Startup", "Liveness", "Readiness") and pr.get("httpGet")), None) or {}
    res = c.get("resources") or {}
    info = AppInfo(
        service_name=name,
        image_uri=c.get("image"),
        container_port=ingress.get("targetPort"),
        cpu_units=_cpu_units(res.get("cpu")),
        memory_mib=_memory_mib(res.get("memory")),
        health_check_path=probe.get("path"),
        env_vars=env_vars,
        secret_refs=secret_refs,
        cloud_run_blockers=blockers,
        cloud_run_notes=notes,
    )
    return info, env_vars.get("PGUSER"), env_vars.get("PGDATABASE")


def _azure_vcpu(sku: str | None) -> int | None:
    """Standard_D2s_v3 → 2, Standard_B1ms → 1. 메모리는 SKU 표가 없으면 모르므로 계산하지 않습니다."""
    m = re.fullmatch(r"Standard_[A-Z]+(\d+)[a-z]*(_v\d+)?", sku or "")
    return int(m.group(1)) if m else None


def collect_azure(inp: AssessmentInput, get_json: GetJson | None = None) -> tuple[DbAssessment, AppAssessment, str]:
    src = _required(inp.cloud_source, "azure")
    app_gaps: list[str] = []
    db_gaps: list[str] = []
    get_json = get_json or _get_json
    token = azure_token(src.credentials_ref)
    h = {"Authorization": f"Bearer {token}"}
    base = f"{AZURE_ARM}/subscriptions/{_seg(src.subscription_id)}/resourceGroups/{_seg(src.resource_group)}/providers"

    app_info, pg_user, pg_db = AppInfo(service_name=src.app_name), None, None
    try:
        app = get_json(f"{base}/Microsoft.App/containerApps/{_seg(src.app_name)}?api-version={AZURE_APPS_API}", h)
        app_info, pg_user, pg_db = _azure_app(app, src.app_name, app_gaps)
    except Exception as e:
        app_gaps.append(f"Container App 조회 실패 ({type(e).__name__})")

    db = DbInfo(engine="postgres")
    source_db = SourceDb(instance_id=src.db_instance)
    network = SourceNetwork()
    server = f"{base}/Microsoft.DBforPostgreSQL/flexibleServers/{_seg(src.db_instance)}"
    try:
        s = get_json(f"{server}?api-version={AZURE_PG_API}", h)
        props = s.get("properties") or {}
        net = props.get("network") or {}
        public = (net.get("publicNetworkAccess") or "").lower() == "enabled"
        host = props.get("fullyQualifiedDomainName")
        sku = (s.get("sku") or {}).get("name")
        wal = get_json(f"{server}/configurations/wal_level?api-version={AZURE_PG_API}", h)
        wal_level = (wal.get("properties") or {}).get("value")
        storage = (props.get("storage") or {}).get("storageSizeGB")
        db = DbInfo(
            engine="postgres",
            publicly_accessible=public,
            engine_version=str(props.get("version")) if props.get("version") else None,
            username=pg_user,
            instance_class=sku,
            allocated_storage_gb=int(storage) if storage else None,
            logical_replication=wal_level == "logical",
            wal_level=wal_level,
            vcpu=_azure_vcpu(sku),
        )
        dbs = get_json(f"{server}/databases?api-version={AZURE_PG_API}", h)
        database = _pick_database([d.get("name") for d in dbs.get("value") or []], pg_db, db_gaps)
        db = db.model_copy(update={"database_name": database})
        source_db = SourceDb(instance_id=src.db_instance, host=host, port=5432, database=database,
                             version=db.engine_version, publicly_accessible=public,
                             logical_replication=db.logical_replication)
        network = SourceNetwork(vpc_id=net.get("delegatedSubnetResourceId"))
        db = _read_tables(inp, host, 5432, database, db, db_gaps)
    except Exception as e:
        db_gaps.append(f"PostgreSQL Flexible Server 조회 실패 ({type(e).__name__})")

    return (DbAssessment(facts=db, source_db=source_db, network=network, gaps=db_gaps),
            AppAssessment(facts=app_info, account_id=src.subscription_id, db_user=pg_user, gaps=app_gaps),
            src.region)
