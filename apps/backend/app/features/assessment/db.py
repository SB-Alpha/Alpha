"""원본 DB·네트워크 조회 통합. (담당: 김준석)

흐름: RDS·네트워크(rds) → 앱 PGDATABASE 힌트(app_hint, 보조) → bastion → DB 관리자 자격(refs) → SSM 터널 → PG 카탈로그(pg_connect/pg_tables).
절대 예외를 던지지 않습니다. 조회 못한 값은 None 으로 두고 gaps 에 한국어 문장(비밀 없음)만 남깁니다.
"""

from __future__ import annotations

import os

from . import app_hint, bastion, pg_connect, pg_tables, rds, refs
from .aws import AwsSession
from .schemas import AssessmentInput, DbAssessment


def _pg_step(inp: AssessmentInput, session: AwsSession, r: rds.RdsResult, gaps: list[str]) -> None:
    """PG 카탈로그 조회 결과를 r(db·source_db)에 반영합니다. 실패는 gaps 로."""
    info, reason = bastion.ensure_bastion(session, inp.aws_region, r.vpc_id)
    if info is None:
        gaps.append(reason or "bastion 을 사용할 수 없음")
        return
    creds, reason = refs.db_admin_for(
        inp.db_admin_ref, session, r.resource_id, r.source_db.instance_id,
        allow_asm_fallback=os.environ.get("ALPHA_DB_ADMIN_ASM_FALLBACK") == "1",
    )
    if creds is None:
        gaps.append(reason or "DB 관리자 자격 증명을 찾지 못함")
        return
    # 우선순위: ALPHA_SOURCE_DB_NAME > ECS PGDATABASE 힌트 > RDS DBName > postgres(폴백)
    explicit = os.environ.get("ALPHA_SOURCE_DB_NAME")
    hint = None if explicit else app_hint.pg_database_hint(
        session, inp.aws_region, r.source_db.host, inp.ecs_cluster, inp.ecs_service)
    named = explicit or hint  # 지정된 이름(폴백 아님)
    database = named or r.db_name or "postgres"
    try:
        with bastion.open_tunnel(session, inp.aws_region, info, remote_host=r.source_db.host,
                                 remote_port=r.source_db.port) as (host, port):
            catalog = pg_connect.collect_catalog(host, port, creds["user"], creds["password"], database)
    except Exception as e:  # 메시지에 비밀번호가 섞일 수 있어 클래스명만 남깁니다
        gaps.append(f"DB 접속 실패 ({type(e).__name__})")
        return
    res = pg_tables.catalog_to_dbinfo(catalog, database)
    gaps += res.gaps
    # 이름을 명시하지 않았는데(폴백 postgres) 사용자 DB 가 따로 있으면 postgres 일치는 검증이 아님
    users = [d for d in catalog.get("databases", []) if d != "postgres"]
    if hint and not res.database_name:  # 지정된 이름이라 postgres 로 대신하지 않고 비움
        res.database_name, res.tables = None, None
        gaps.append(f"앱이 쓰는 DB({hint})를 찾지 못함")
    elif not named and not r.db_name and users:
        res.database_name, res.tables = None, None
        gaps.append(f"앱 DB 이름을 확정하지 못함 (후보: {', '.join(users)}) — ALPHA_SOURCE_DB_NAME 지정 필요")
    update: dict = {"database_name": res.database_name, "wal_level": res.wal_level,
                    "max_replication_slots": res.max_replication_slots,
                    "replication_slots_in_use": res.replication_slots_in_use}
    if res.tables is not None:
        update["tables"] = res.tables
    r.db = r.db.model_copy(update=update)
    if res.database_name:
        r.source_db = r.source_db.model_copy(update={"database": res.database_name})


def collect(inp: AssessmentInput, session: AwsSession) -> DbAssessment:
    gaps: list[str] = []
    r = rds.RdsResult()
    try:
        r = rds.collect_rds(session, inp.aws_region, inp.rds_instance_id)
        gaps += r.gaps
        if r.source_db.instance_id is not None:
            _pg_step(inp, session, r, gaps)
    except Exception as e:
        gaps.append(f"DB·네트워크 조회 중 오류 ({type(e).__name__})")
    return DbAssessment(facts=r.db, source_db=r.source_db, network=r.network, gaps=gaps)
