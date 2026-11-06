"""GCP Cloud SQL 수집기: 가짜 sqladmin 응답으로 검증 (SDK·실환경 불필요)."""

import pytest

from app.features.assessment import db, gcp_sql
from app.features.assessment.schemas import AssessmentInput

KW = dict(aws_credentials_ref="profile:x", aws_region="ap-northeast-2", target_region="asia-northeast3",
          source_cloud="gcp", rds_instance_id="pg1")


class HttpError(Exception):
    def __init__(self, status):
        self.resp = type("R", (), {"status": status})()


def fake(resp=None, exc=None):
    class Req:
        def execute(self):
            if exc:
                raise exc
            return resp

    class Inst:
        def get(self, **kw):
            assert kw == {"project": "p1", "instance": "pg1"}
            return Req()

    class C:
        def instances(self):
            return Inst()

    return lambda session: C()


def inst(flag=None, **over):
    d = {"name": "pg1", "databaseVersion": "POSTGRES_15", "connectionName": "p1:r:pg1",
         "ipAddresses": [{"type": "PRIMARY", "ipAddress": "34.1.1.1"}, {"type": "PRIVATE", "ipAddress": "10.0.0.5"}],
         "settings": {"tier": "db-custom-2-7680", "dataDiskSizeGb": "100",
                      "ipConfiguration": {"ipv4Enabled": False, "privateNetwork": "projects/p1/global/networks/vpc"},
                      "databaseFlags": [{"name": "cloudsql.logical_decoding", "value": flag}] if flag else []}}
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_GCP_PROJECT", "p1")
    monkeypatch.setenv("ALPHA_ALLOW_ENV_REFS", "1")
    monkeypatch.setenv("X_USER", "u")
    monkeypatch.setenv("X_PASSWORD", "pw-secret")


def run(client, ref="env://X", **kw):
    tf = lambda conn, private: (lambda: (conn, private))
    return gcp_sql.collect(AssessmentInput(db_admin_ref=ref, **KW), None, client, tf)


def test_ok():
    s = run(fake(inst("on")))
    assert (s.db.engine, s.db.engine_version, s.db.instance_class, s.db.allocated_storage_gb) == ("postgres", "15", "db-custom-2-7680", 100)
    assert s.db.logical_replication is True and s.db.publicly_accessible is False
    assert (s.source_db.instance_id, s.source_db.host, s.source_db.port) == ("pg1", "10.0.0.5", 5432)
    assert s.network.vpc_id.endswith("/vpc")
    assert s.creds == {"user": "u", "password": "pw-secret"} and s.gaps == []
    assert s.tunnel() == ("p1:r:pg1", True)


@pytest.mark.parametrize("flag,want", [("off", False), (None, False), ("on", True)])
def test_flag(flag, want):
    assert run(fake(inst(flag))).db.logical_replication is want


def test_permission_denied():
    s = run(fake(exc=HttpError(403)))
    assert s.source_db.instance_id is None and s.tunnel is None
    assert s.gaps == ["Cloud SQL 조회 실패 (HttpError 403)"]


def test_not_found():
    assert run(fake(exc=HttpError(404))).gaps == ["Cloud SQL 조회 실패 (HttpError 404)"]


def test_no_project(monkeypatch):
    monkeypatch.delenv("ALPHA_GCP_PROJECT")
    s = run(fake(inst()))
    assert s.source_db.instance_id is None and "ALPHA_GCP_PROJECT" in s.gaps[0]


def test_no_creds_gap_without_secret():
    s = run(fake(inst("on")), ref=None)
    assert s.creds is None and any("db_admin_ref" in g for g in s.gaps)
    assert "pw-secret" not in "".join(s.gaps)


def test_not_postgres():
    s = run(fake(inst(databaseVersion="MYSQL_8_0")))
    assert s.db.engine is None and any("PostgreSQL" in g for g in s.gaps)


def test_db_collect_never_raises():
    out = db.collect(AssessmentInput(**KW), object())  # SDK 미설치/자격 없음도 gaps
    assert out.gaps and out.source_db.instance_id is None
