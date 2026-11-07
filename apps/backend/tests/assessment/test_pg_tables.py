"""pg_tables 테스트. 가짜 cursor만 사용하며 실제 DB에는 접속하지 않는다."""

from app.features.assessment import pg_tables as m


class FakeCursor:
    """쿼리 문자열 부분일치로 미리 정한 rows를 반환. fail에 든 키가 매칭되면 예외."""

    def __init__(self, responses, fail=()):
        self.responses, self.fail, self.rows = responses, set(fail), []

    def execute(self, sql):
        for key, rows in self.responses.items():
            if key in sql:
                if key in self.fail:
                    raise PermissionError("permission denied")
                self.rows = rows
                return
        raise AssertionError(f"unexpected sql: {sql}")

    def fetchall(self):
        return self.rows


def base(tables):
    return {
        "FROM pg_database": [("appdb",), ("postgres",)],
        "current_database()": [("appdb",)],
        "SHOW wal_level": [("logical",)],
        "SHOW max_replication_slots": [("10",)],
        "pg_replication_slots": [(2,)],
        "FROM pg_class": tables,
        "rolreplication": [(True,)],
    }


WORK = ("public", "work_items", "app", "d", 120.0, ["id"])


def test_normal():
    c = m.collect_pg_catalog(FakeCursor(base([WORK])))
    assert c["gaps"] == []
    assert c["databases"] == ["appdb", "postgres"] and c["current_database"] == "appdb"
    assert c["wal_level"] == "logical" and c["max_replication_slots"] == 10
    assert c["slots_in_use"] == 2 and c["can_replicate"] is True
    assert c["tables"] == [{
        "schema": "public", "name": "work_items", "qualified": "public.work_items",
        "has_primary_key": True, "primary_key_columns": ["id"],
        "replica_identity": "default", "row_estimate": 120, "owner": "app",
    }]
    assert m.to_source_tables(c) == ["public.work_items"]


def test_no_pk_kept_and_gap():
    c = m.collect_pg_catalog(FakeCursor(base([WORK, ("public", "logs", "app", "d", 5.0, None)])))
    assert m.to_source_tables(c) == ["public.work_items", "public.logs"]
    assert m.tables_without_pk(c) == ["public.logs"]
    m.tables_without_pk(c)  # 중복 호출해도 gap은 하나
    assert c["gaps"] == [{"item": "table:public.logs", "reason": "PK 없음, 이관 전 확인 필요"}]


def test_composite_pk_and_replica_identity():
    c = m.collect_pg_catalog(FakeCursor(base([("public", "m", "app", "f", 1.0, "{a,b}")])))
    t = c["tables"][0]
    assert t["primary_key_columns"] == ["a", "b"] and t["replica_identity"] == "full"


def test_one_query_fails():
    c = m.collect_pg_catalog(FakeCursor(base([WORK]), fail=["pg_replication_slots"]))
    assert c["slots_in_use"] is None and c["wal_level"] == "logical" and len(c["tables"]) == 1
    assert len(c["gaps"]) == 1
    g = c["gaps"][0]
    assert g["item"] == "slots_in_use" and "PermissionError" in g["reason"]


def test_row_estimate_minus_one():
    c = m.collect_pg_catalog(FakeCursor(base([("public", "t", "app", "d", -1.0, ["id"])])))
    assert c["tables"][0]["row_estimate"] is None


def test_wal_level_parse():
    r = base([])
    r["SHOW wal_level"] = [(" replica ",)]
    assert m.collect_pg_catalog(FakeCursor(r))["wal_level"] == "replica"


def test_system_schemas_excluded_in_sql():
    # 가짜 cursor는 필터링하지 않으므로, 제외 조건이 쿼리에 있는지 확인
    for s in ("pg_catalog", "information_schema", "pg_toast"):
        assert s in m.SQL_TABLES
    assert "relkind IN ('r', 'p')" in m.SQL_TABLES


def test_psql_bool_strings():
    # psql 폴백은 't'/'f' 문자열을 준다: 'f'가 True가 되면 안 됨
    for raw, want in (("t", True), ("true", True), ("on", True), ("1", True),
                      ("f", False), ("false", False), ("off", False), ("0", False), (None, None)):
        r = {**base([WORK]), "rolreplication": [(raw,)]}
        assert m.collect_pg_catalog(FakeCursor(r))["can_replicate"] is want, raw
    r = {**base([WORK]), "rolreplication": [("maybe",)]}
    c = m.collect_pg_catalog(FakeCursor(r))
    assert c["can_replicate"] is None and c["gaps"][0]["item"] == "can_replicate"


# --- catalog_to_dbinfo ---
def _cat(rows=None, **kw):
    return m.collect_pg_catalog(FakeCursor(base([WORK] if rows is None else rows), **kw))


def test_dbinfo_normal():
    r = m.catalog_to_dbinfo(_cat(), "appdb")
    assert r.database_name == "appdb" and r.wal_level == "logical" and r.gaps == []
    t = r.tables[0]
    assert (t.schema_name, t.name, t.row_count, t.has_primary_key) == ("public", "work_items", 120, True)


def test_dbinfo_db_mismatch_and_none():
    for req in ("other", None):
        r = m.catalog_to_dbinfo(_cat(), req)
        assert r.database_name is None and "요청한 DB와 조회한 DB가 다름" in r.gaps


def test_dbinfo_tables_failure_is_none():
    r = m.catalog_to_dbinfo(_cat(fail=["FROM pg_class"]), "appdb")
    assert r.tables is None and r.gaps == ["PG 카탈로그 tables 조회 실패 (PermissionError)"]


def test_dbinfo_empty_tables():
    assert m.catalog_to_dbinfo(_cat(rows=[]), "appdb").tables == []


def test_dbinfo_row_estimate_minus_one_and_no_pk():
    r = m.catalog_to_dbinfo(_cat(rows=[("public", "t", "app", "d", -1.0, None)]), "appdb")
    assert r.tables[0].row_count is None and r.tables[0].has_primary_key is False
    assert r.gaps == []  # PK 판정은 rules.py


def test_dbinfo_gaps_are_str_without_secrets():
    r = m.catalog_to_dbinfo(_cat(fail=["SHOW wal_level", "pg_replication_slots"]), "appdb")
    assert r.wal_level is None and len(r.gaps) == 2
    assert all(isinstance(g, str) and "permission denied" not in g for g in r.gaps)


def test_first_query_failure_aborts_remaining():
    """접속 불가(첫 쿼리 실패)면 나머지 쿼리를 시도하지 않고 예외로 올린다."""
    import pytest
    from app.features.assessment import pg_tables

    calls = []

    class Cur:
        def execute(self, sql):
            calls.append(sql)
            raise RuntimeError("could not connect to server")

        def fetchall(self):
            return []

    with pytest.raises(RuntimeError):
        pg_tables.collect_pg_catalog(Cur())
    assert len(calls) == 1


def test_dbinfo_carries_replication_slots():
    r = m.catalog_to_dbinfo(_cat(), "appdb")
    assert r.max_replication_slots == 10 and r.replication_slots_in_use is not None


def test_dbinfo_drops_slots_when_either_query_failed():
    r = m.catalog_to_dbinfo(_cat(fail=["pg_replication_slots"]), "appdb")
    assert r.max_replication_slots is None and r.replication_slots_in_use is None
