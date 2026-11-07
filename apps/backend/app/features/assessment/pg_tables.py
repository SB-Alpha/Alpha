"""원본 RDS(PostgreSQL 16)에 접속한 cursor로 CDC 이관에 필요한 카탈로그 정보를 읽는다.

- DB 드라이버를 import하지 않는다. DB-API 호환 cursor(`execute`, `fetchall`)를 인자로 받는다.
- 읽기 전용 SELECT/SHOW만 사용한다. 비밀값은 다루지 않는다.
- 항목마다 독립적으로 실행한다. 실패하면 값은 None(또는 빈 목록)이고 gaps에 사유를 남긴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.features.assessment.schemas import TableInfo

# rdsadmin·cloudsqladmin·azure_*: 각 CSP 관리용 내부 DB, 템플릿 DB: 이관 대상 아님
SQL_DATABASES = (
    "SELECT datname FROM pg_database "
    "WHERE NOT datistemplate AND datname NOT IN ('rdsadmin', 'cloudsqladmin', 'azure_maintenance', 'azure_sys') "
    "ORDER BY datname"
)
SQL_CURRENT_DB = "SELECT current_database()"
SQL_WAL_LEVEL = "SHOW wal_level"
SQL_MAX_SLOTS = "SHOW max_replication_slots"
SQL_SLOTS_IN_USE = "SELECT count(*) FROM pg_replication_slots"
# CDC 에이전트 카탈로그(infra/db_migration/agent/cdc_agent/catalog.py)와 같은 대상·기준: alpha_cdc(마커)와
# 확장 소유 테이블은 뺀다. 파티션·SELECT 권한은 CDC 가 거절하므로 함께 읽어 gap 으로 알린다.
# PK 컬럼은 indkey 순서대로 정렬. reltuples는 통계 기반 추정치(미분석 테이블은 -1).
SQL_TABLES = """
SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner), c.relreplident, c.reltuples,
       (SELECT array_agg(a.attname ORDER BY array_position(i.indkey::int2[], a.attnum))
          FROM pg_index i
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = c.oid AND i.indisprimary) AS pk_columns,
       c.relkind, c.relispartition,
       has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p')
   AND n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema', 'alpha_cdc')
   AND NOT EXISTS (SELECT 1 FROM pg_depend d
                    WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
 ORDER BY n.nspname, c.relname
"""
# 복제 권한을 주는 역할. AWS 는 rds_superuser 가 rds_replication 의 멤버라 마스터 계정이 상속으로 얻으므로
# 직접 멤버가 아니라 pg_has_role(USAGE) 로 상속까지 본다(실 RDS PG16 에서 확인). GCP/Azure 는 관리자 역할
# (cloudsqlsuperuser, azure_pg_admin)이 REPLICATION 속성을 주지 않음이 Azure 실환경에서 확인돼 속성만 본다.
# GCP 실환경(Cloud SQL PG16, 2026-11-07) 확인: cloudsqlsuperuser·postgres 모두 rolreplication=false 라 속성만 보는 판정이 맞다.
# CDC 계정은 REPLICATION 속성을 따로 줘야 한다(ALTER ROLE … WITH REPLICATION, 아직 CDC 계정으로는 미시험).
REPLICATION_ROLES = {"aws": ("rds_replication",), "gcp": (), "azure": ()}


def sql_can_replicate(cloud: str = "aws") -> str:
    roles = ", ".join(f"'{r}'" for r in REPLICATION_ROLES[cloud])  # 고정 상수라 주입 위험 없음
    # 역할이 없으면 pg_roles 에서 0행이라 pg_has_role 이 호출되지 않아 오류가 나지 않음
    member = f""" OR EXISTS (
         SELECT 1 FROM pg_roles g
          WHERE g.rolname IN ({roles}) AND pg_has_role(r.oid, g.oid, 'USAGE'))""" if roles else ""
    return f"""
SELECT r.rolreplication OR r.rolsuper{member}
  FROM pg_roles r WHERE r.rolname = current_user
"""


_REPLIDENT = {"d": "default", "f": "full", "n": "nothing", "i": "index"}


def _one(cur: Any, sql: str) -> Any:
    cur.execute(sql)
    return cur.fetchall()[0][0]


def _bool(v: Any) -> bool | None:
    """psql 폴백은 불리언을 't'/'f' 문자열로 준다. bool('f')==True 함정을 피한다."""
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("t", "true", "on", "1"):
            return True
        if t in ("f", "false", "off", "0"):
            return False
        raise ValueError(f"불리언으로 해석할 수 없는 값: {t[:20]!r}")
    return bool(v)


def _pk_cols(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):  # 드라이버가 배열을 '{a,b}' 문자열로 줄 때
        v = v.strip("{}").split(",")
    return list(v)


def _row_estimate(v: Any) -> int | None:
    # reltuples는 추정치(실제 count 아님). -1은 한 번도 분석되지 않음 -> 모름(None)
    return None if v is None or float(v) < 0 else int(v)


def collect_pg_catalog(cur: Any, cloud: str = "aws") -> dict:
    """카탈로그 정보를 수집한다. 실패한 항목은 None/[]로 두고 gaps에 사유를 남긴다."""
    out: dict[str, Any] = {
        "databases": [], "current_database": None, "wal_level": None,
        "max_replication_slots": None, "slots_in_use": None,
        "tables": [], "can_replicate": None, "gaps": [],
    }

    def step(item: str, fn, first: bool = False):
        try:
            out[item] = fn()
        except Exception as e:  # noqa: BLE001 - 사유를 gaps에 기록하므로 삼키는 것이 아님
            if first:  # 첫 쿼리 실패 = 접속 불가로 보고 중단(나머지 쿼리 반복 시 최악 시간 누적)
                raise
            out["gaps"].append({"item": item, "reason": f"{type(e).__name__}: {e}"})

    def rows(sql):
        cur.execute(sql)
        return cur.fetchall()

    step("databases", lambda: [r[0] for r in rows(SQL_DATABASES)], first=True)
    step("current_database", lambda: _one(cur, SQL_CURRENT_DB))
    step("wal_level", lambda: str(_one(cur, SQL_WAL_LEVEL)).strip())
    step("max_replication_slots", lambda: int(_one(cur, SQL_MAX_SLOTS)))
    step("slots_in_use", lambda: int(_one(cur, SQL_SLOTS_IN_USE)))
    step("can_replicate", lambda: _bool(_one(cur, sql_can_replicate(cloud))))

    def tables():
        res = []
        for schema, name, owner, ident, tuples, pk, kind, is_part, can_select in rows(SQL_TABLES):
            cols = _pk_cols(pk)
            res.append({
                "schema": schema, "name": name, "qualified": f"{schema}.{name}",
                "has_primary_key": bool(cols), "primary_key_columns": cols,
                "replica_identity": _REPLIDENT.get(ident, str(ident)),
                "row_estimate": _row_estimate(tuples), "owner": owner,
                "partitioned": kind == "p" or _bool(is_part) is True, "can_select": _bool(can_select),
            })
        return res

    step("tables", tables)
    return out


def to_source_tables(catalog: dict) -> list[str]:
    """명세의 `source.tables` 모양. PK 없는 테이블도 제외하지 않고 포함한다."""
    return [t["qualified"] for t in catalog["tables"]]


def tables_without_pk(catalog: dict) -> list[str]:
    """PK 없는 테이블 목록을 반환하고, 있으면 catalog["gaps"]에 확인 필요 항목을 추가한다."""
    names = [t["qualified"] for t in catalog["tables"] if not t["has_primary_key"]]
    for q in names:
        gap = {"item": f"table:{q}", "reason": "PK 없음, 이관 전 확인 필요"}
        if gap not in catalog["gaps"]:
            catalog["gaps"].append(gap)
    return names


_CDC_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")  # job_request.py 와 같은 규칙


def cdc_refusals(tables: list[dict]) -> list[str]:
    """CDC(db_migration)가 작업 등록 때 거절하는 테이블. 승인 전에 미리 알린다. PK 없음은 rules.py 가 판정."""
    checks = (("파티션 테이블은 CDC 가 지원하지 않음", lambda t: t.get("partitioned")),
              ("원본 계정이 SELECT 할 수 없음", lambda t: t.get("can_select") is False),
              ("테이블 이름이 소문자 규칙이 아님", lambda t: not _CDC_TABLE_NAME.fullmatch(t["qualified"])))
    out = []
    for why, bad in checks:
        names = [t["qualified"] for t in tables if bad(t)]
        if names:
            out.append(f"{why}: {', '.join(names[:5])}" + (f" 외 {len(names) - 5}개" if len(names) > 5 else ""))
    return out


@dataclass
class CatalogResult:
    """카탈로그 -> DbInfo 칸 변환 결과. tables=None 은 조회 실패(빈 리스트는 테이블 없음)."""

    database_name: str | None = None
    wal_level: str | None = None
    max_replication_slots: int | None = None
    replication_slots_in_use: int | None = None
    tables: list[TableInfo] | None = None
    gaps: list[str] = field(default_factory=list)


def catalog_to_dbinfo(catalog: dict, requested_database: str | None) -> CatalogResult:
    """collect_pg_catalog 결과를 DbInfo 칸으로 옮긴다. gap 에는 항목과 예외 클래스명만 남긴다."""
    failed = {g["item"] for g in catalog["gaps"]}
    res = CatalogResult()
    for g in catalog["gaps"]:
        cls = str(g["reason"]).split(":", 1)[0]  # 이유 전문 대신 클래스명만
        res.gaps.append(f"PG 카탈로그 {g['item']} 조회 실패 ({cls})")
    cur = catalog.get("current_database")
    if (requested_database and "current_database" not in failed and cur == requested_database
            and "databases" not in failed and cur in catalog["databases"]):
        res.database_name = cur
    else:
        res.gaps.append("요청한 DB와 조회한 DB가 다름")
    if "wal_level" not in failed:
        res.wal_level = catalog.get("wal_level")
    if "max_replication_slots" not in failed and "slots_in_use" not in failed:
        res.max_replication_slots = catalog.get("max_replication_slots")
        res.replication_slots_in_use = catalog.get("slots_in_use")
    if "tables" not in failed:
        res.gaps += cdc_refusals(catalog["tables"])
        res.tables = [TableInfo(schema_name=t["schema"], name=t["name"], row_count=t["row_estimate"],
                                has_primary_key=t["has_primary_key"]) for t in catalog["tables"]]
    return res
