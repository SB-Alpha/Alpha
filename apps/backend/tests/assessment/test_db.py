"""db.py 통합 테스트: rds·bastion·refs·pg_connect 는 가짜로 바꿉니다 (AWS·DB 접속 없음)."""

from contextlib import contextmanager

import pytest

from app.features.assessment import app as app_part
from app.features.assessment import app_hint, bastion, db, pg_connect, rds, refs, service
from app.features.assessment.schemas import (
    AppAssessment, AppInfo, AssessmentInput, DbAssessment, DbInfo, SourceDb, SourceNetwork,
)

from .fakes import session

SENTINEL = "PW-SENTINEL-9f3a"
INP = AssessmentInput(aws_credentials_ref="profile:x", aws_region="ap-northeast-2", target_region="asia-northeast3")
CATALOG = {
    "databases": ["appdb", "postgres"], "current_database": "appdb", "wal_level": "logical",
    "tables": [{"schema": "public", "name": "t", "qualified": "public.t", "row_estimate": 3, "has_primary_key": True}],
    "gaps": [],
}


def _rds(**kw):
    base = dict(
        db=DbInfo(engine="postgres", engine_version="16.15", logical_replication=True),
        source_db=SourceDb(instance_id="i1", host="h.example.com", port=5432),
        network=SourceNetwork(vpc_id="vpc-1", cidr="172.31.0.0/16"), resource_id="db-R", vpc_id="vpc-1", db_name="appdb",
    )
    base.update(kw)
    return rds.RdsResult(**base)


@pytest.fixture
def fx(monkeypatch):
    s = {"closed": 0, "catalog": CATALOG, "rds": _rds(), "bastion": (bastion.BastionInfo("i-b", "vpc-1", None, True), None),
         "creds": ({"user": "master", "password": SENTINEL}, None), "exc": None, "args": None}
    monkeypatch.delenv("ALPHA_SOURCE_DB_NAME", raising=False)
    s["hint"] = None
    monkeypatch.setattr(app_hint, "pg_database_hint", lambda *a, **k: s["hint"])
    monkeypatch.setattr(rds, "collect_rds", lambda *a, **k: s["rds"])
    monkeypatch.setattr(bastion, "ensure_bastion", lambda *a, **k: s["bastion"])
    monkeypatch.setattr(refs, "db_admin_for", lambda *a, **k: s["creds"])

    @contextmanager
    def tunnel(*a, **k):
        try:
            yield "localhost", 15432
        finally:
            s["closed"] += 1

    def catalog(host, port, user, password, database, cloud="aws"):
        s["args"] = (host, port, user, database)
        if s["exc"]:
            raise s["exc"]
        return s["catalog"]

    monkeypatch.setattr(bastion, "open_tunnel", tunnel)
    monkeypatch.setattr(pg_connect, "collect_catalog", catalog)
    return s


def test_success_mapping(fx):
    out = db.collect(INP, object())
    DbAssessment.model_validate(out.model_dump())
    assert out.gaps == []
    assert out.facts.database_name == "appdb" and out.facts.wal_level == "logical"
    assert [t.name for t in out.facts.tables] == ["t"]
    assert out.source_db.database == "appdb" and out.network.cidr == "172.31.0.0/16"
    assert fx["args"] == ("localhost", 15432, "master", "appdb") and fx["closed"] == 1
    assert SENTINEL not in out.model_dump_json()


def test_no_instance(fx):
    fx["rds"] = rds.RdsResult(gaps=["RDS 인스턴스를 찾지 못함"])
    out = db.collect(INP, object())
    assert out.gaps == ["RDS 인스턴스를 찾지 못함"] and fx["args"] is None


def test_no_bastion_and_no_creds(fx):
    fx["bastion"] = (None, "bastion 없음")
    out = db.collect(INP, object())
    assert out.gaps == ["bastion 없음"] and out.facts.tables == [] and out.facts.wal_level is None
    fx["bastion"] = (bastion.BastionInfo("i-b", None, None, True), None)
    fx["creds"] = (None, "db_admin_ref 없음")
    assert db.collect(INP, object()).gaps == ["db_admin_ref 없음"] and fx["args"] is None


def test_pg_error_only_class_name_and_tunnel_closed(fx):
    fx["exc"] = RuntimeError(f"auth failed {SENTINEL}")
    out = db.collect(INP, object())
    assert out.gaps == ["DB 접속 실패 (RuntimeError)"] and fx["closed"] == 1
    assert SENTINEL not in out.model_dump_json() and out.facts.database_name is None


def test_tunnel_error_is_gap(fx, monkeypatch):
    def bad(*a, **k):
        raise bastion.BastionError(SENTINEL)

    monkeypatch.setattr(bastion, "open_tunnel", bad)
    assert db.collect(INP, object()).gaps == ["DB 접속 실패 (BastionError)"]


def test_fallback_with_user_dbs_not_confirmed(fx):
    fx["rds"] = _rds(db_name=None)
    fx["catalog"] = {**CATALOG, "databases": ["a", "appdb", "postgres"], "current_database": "postgres", "tables": []}
    out = db.collect(INP, object())
    assert out.facts.database_name is None and out.source_db.database is None and out.facts.tables == []  # 갱신 안 함(기본값)
    assert any("앱 DB 이름을 확정하지 못함 (후보: a, appdb)" in g and "ALPHA_SOURCE_DB_NAME" in g for g in out.gaps)


def test_fallback_postgres_only_confirmed(fx):
    fx["rds"] = _rds(db_name=None)
    fx["catalog"] = {**CATALOG, "databases": ["postgres"], "current_database": "postgres"}
    out = db.collect(INP, object())
    assert out.facts.database_name == "postgres" and out.source_db.database == "postgres"
    assert [t.name for t in out.facts.tables] == ["t"] and out.gaps == []


def test_explicit_match_confirmed(fx, monkeypatch):
    monkeypatch.setenv("ALPHA_SOURCE_DB_NAME", "appdb")
    fx["catalog"] = {**CATALOG, "databases": ["a", "appdb", "postgres"]}
    out = db.collect(INP, object())
    assert fx["args"][3] == "appdb" and out.facts.database_name == "appdb" and out.gaps == []


def test_explicit_mismatch_not_confirmed(fx, monkeypatch):
    monkeypatch.setenv("ALPHA_SOURCE_DB_NAME", "appdb")
    fx["catalog"] = {**CATALOG, "current_database": "other"}
    out = db.collect(INP, object())
    assert out.facts.database_name is None and out.source_db.database is None


def test_hint_beats_rds_dbname(fx):
    fx["hint"] = "apphint"
    fx["catalog"] = {**CATALOG, "databases": ["apphint", "appdb", "postgres"], "current_database": "apphint"}
    out = db.collect(INP, object())
    assert fx["args"][3] == "apphint" and out.facts.database_name == "apphint"
    assert out.source_db.database == "apphint" and out.gaps == []


def test_env_beats_hint(fx, monkeypatch):
    monkeypatch.setenv("ALPHA_SOURCE_DB_NAME", "appdb")
    fx["hint"] = "apphint"
    assert db.collect(INP, object()).facts.database_name == "appdb" and fx["args"][3] == "appdb"


def test_no_hint_uses_old_fallback(fx):
    fx["rds"] = _rds(db_name=None)
    fx["catalog"] = {**CATALOG, "databases": ["postgres"], "current_database": "postgres"}
    out = db.collect(INP, object())
    assert fx["args"][3] == "postgres" and out.facts.database_name == "postgres"


def test_hint_db_missing_clears_with_gap(fx):
    fx["rds"] = _rds(db_name=None)
    fx["hint"] = "apphint"
    fx["catalog"] = {**CATALOG, "databases": ["postgres"], "current_database": "postgres"}
    out = db.collect(INP, object())
    assert out.facts.database_name is None and out.source_db.database is None and out.facts.tables == []
    assert any("앱이 쓰는 DB(apphint)를 찾지 못함" in g for g in out.gaps)


def test_requested_db_mismatch(fx):
    fx["catalog"] = {**CATALOG, "current_database": "other"}
    out = db.collect(INP, object())
    assert out.facts.database_name is None and out.source_db.database is None
    assert out.gaps == ["요청한 DB와 조회한 DB가 다름"]


def test_unexpected_exception_never_raises(monkeypatch):
    monkeypatch.setattr(rds, "collect_rds", lambda *a, **k: 1 / 0)
    assert db.collect(INP, object()).gaps == ["DB·네트워크 조회 중 오류 (ZeroDivisionError)"]


def test_invalid_peer_cidr_is_gap_not_exception():
    from .fakes import FakeClient, FakeSession
    inst = {"DBInstanceIdentifier": "i1", "DBSubnetGroup": {"VpcId": "vpc-1", "Subnets": []}}
    sess = FakeSession(
        rds=FakeClient({"describe_db_instances": {"DBInstances": [inst]}}),
        ec2=FakeClient({"describe_vpcs": {"Vpcs": [{"CidrBlock": "10.0.0.0/16"}]},
                        "describe_route_tables": {"RouteTables": []}}),
    )
    r = rds.collect_rds(sess, "ap-northeast-2", "i1", peer_cidrs=("not-a-cidr", "10.50.0.1/8"))
    assert any("형식이 올바르지 않아" in g for g in r.gaps)
    assert any("겹침" in g for g in r.gaps)  # host bits 가 켜진 10.50.0.1/8 은 strict=False 로 처리


def test_service_run_fills_tables(fx, monkeypatch):
    out = service.run(INP, session=session())
    assert out.source.tables == ["public.t"] and out.source.db.database == "appdb"
    assert SENTINEL not in out.model_dump_json()
