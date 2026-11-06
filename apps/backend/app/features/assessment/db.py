"""원본 DB·네트워크 조회 통합. (담당: 김준석)

흐름: CSP 수집기(인스턴스·네트워크·접근 경로·관리자 자격; csp.CspSource) → PG 카탈로그(pg_connect/pg_tables, CSP 독립).
CSP 수집기: aws=이 파일의 _collect_aws, gcp=gcp_sql.collect, azure=azure_pg.collect. inp.source_cloud 로 고릅니다.
절대 예외를 던지지 않습니다. 조회 못한 값은 None 으로 두고 gaps 에 한국어 문장(비밀 없음)만 남깁니다.
"""

from __future__ import annotations

import os

from . import app_hint, azure_pg, bastion, gcp_sql, pg_connect, pg_tables, rds, refs
from .aws import AwsSession
from .csp import CspSource
from .schemas import AssessmentInput, DbAssessment


def _collect_aws(inp: AssessmentInput, session: AwsSession) -> CspSource:
    r = rds.collect_rds(session, inp.aws_region, inp.rds_instance_id)
    src = CspSource(db=r.db, source_db=r.source_db, network=r.network, db_name=r.db_name, gaps=list(r.gaps))
    if r.source_db.instance_id is None:
        return src
    try:
        info, reason = bastion.ensure_bastion(session, inp.aws_region, r.vpc_id)
        if info is None:
            src.gaps.append(reason or "bastion 을 사용할 수 없음")
            return src
        creds, reason = refs.db_admin_for(
            inp.db_admin_ref, session, r.resource_id, r.source_db.instance_id,
            allow_asm_fallback=os.environ.get("ALPHA_DB_ADMIN_ASM_FALLBACK") == "1",
        )
        if creds is None:
            src.gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
            return src
        src.creds = creds
        if not os.environ.get("ALPHA_SOURCE_DB_NAME"):
            src.db_hint = app_hint.pg_database_hint(
                session, inp.aws_region, r.source_db.host, inp.ecs_cluster, inp.ecs_service)
        src.tunnel = lambda: bastion.open_tunnel(
            session, inp.aws_region, info, remote_host=r.source_db.host, remote_port=r.source_db.port)
    except Exception as e:
        src.gaps.append(f"DB·네트워크 조회 중 오류 ({type(e).__name__})")
    return src


def _collect_gcp(inp: AssessmentInput, session) -> CspSource:
    return gcp_sql.collect(inp, session)


def _collect_azure(inp: AssessmentInput, session) -> CspSource:
    return azure_pg.collect(inp, session)


# 모듈 속성을 호출 시점에 찾도록 함수로 감쌈 (테스트·B/C 가 교체하기 쉬움)
COLLECTORS = {"aws": _collect_aws, "gcp": _collect_gcp, "azure": _collect_azure}


def _pg_step(inp: AssessmentInput, src: CspSource, gaps: list[str]) -> None:
    """PG 카탈로그 조회 결과를 src(db·source_db)에 반영합니다. 실패는 gaps 로."""
    if src.tunnel is None or src.creds is None:
        return  # 이유는 수집기가 gaps 에 남김
    # 우선순위: ALPHA_SOURCE_DB_NAME > 앱 설정 힌트 > 인스턴스 DBName > postgres(폴백)
    explicit = os.environ.get("ALPHA_SOURCE_DB_NAME")
    hint = None if explicit else src.db_hint
    named = explicit or hint  # 지정된 이름(폴백 아님)
    database = named or src.db_name or "postgres"
    creds = src.creds
    try:
        with src.tunnel() as (host, port):
            catalog = pg_connect.collect_catalog(
                host, port, creds["user"], creds["password"], database, cloud=inp.source_cloud)
    except Exception as e:  # 메시지에 비밀번호가 섞일 수 있어 클래스명만 남깁니다
        gaps.append(f"DB 접속 실패 ({type(e).__name__})")
        return
    res = pg_tables.catalog_to_dbinfo(catalog, database)
    gaps += res.gaps
    if catalog.get("can_replicate") is False:
        gaps.append("DB 관리자 계정에 복제 권한이 없음")
    # 이름을 명시하지 않았는데(폴백 postgres) 사용자 DB 가 따로 있으면 postgres 일치는 검증이 아님
    users = [d for d in catalog.get("databases", []) if d != "postgres"]
    if hint and not res.database_name:  # 지정된 이름이라 postgres 로 대신하지 않고 비움
        res.database_name, res.tables = None, None
        gaps.append(f"앱이 쓰는 DB({hint})를 찾지 못함")
    elif not named and not src.db_name and users:
        res.database_name, res.tables = None, None
        gaps.append(f"앱 DB 이름을 확정하지 못함 (후보: {', '.join(users)}) — ALPHA_SOURCE_DB_NAME 지정 필요")
    update: dict = {"database_name": res.database_name, "wal_level": res.wal_level,
                    "max_replication_slots": res.max_replication_slots,
                    "replication_slots_in_use": res.replication_slots_in_use}
    if res.tables is not None:
        update["tables"] = res.tables
    src.db = src.db.model_copy(update=update)
    if res.database_name:
        src.source_db = src.source_db.model_copy(update={"database": res.database_name})


def collect(inp: AssessmentInput, session) -> DbAssessment:
    gaps: list[str] = []
    src = CspSource()
    try:
        collector = COLLECTORS.get(inp.source_cloud)
        if collector is None:
            gaps.append(f"지원하지 않는 원본 클라우드: {inp.source_cloud}")
        else:
            src = collector(inp, session)
            gaps += src.gaps
            if src.source_db.instance_id is not None:
                _pg_step(inp, src, gaps)
    except Exception as e:
        gaps.append(f"DB·네트워크 조회 중 오류 ({type(e).__name__})")
    return DbAssessment(facts=src.db, source_db=src.source_db, network=src.network, gaps=gaps)
