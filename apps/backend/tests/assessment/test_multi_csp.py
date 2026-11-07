"""source_cloud 분기: 기본값, stub CSP 의 gaps, CSP별 복제 역할·논리 복제 플래그."""

import pytest

from app.features.assessment import db, pg_tables
from app.features.assessment.csp import logical_enabled
from app.features.assessment.schemas import AssessmentInput

KW = dict(aws_credentials_ref="profile:x", aws_region="ap-northeast-2", target_region="asia-northeast3")


def test_source_cloud_default_aws():
    assert AssessmentInput(**KW).source_cloud == "aws"


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_csp_without_collector_falls_to_gaps(cloud, monkeypatch):
    monkeypatch.delitem(db.COLLECTORS, cloud)
    out = db.collect(AssessmentInput(source_cloud=cloud, **KW), object())
    assert out.gaps and out.facts.engine is None and out.source_db.instance_id is None


def test_three_csps_same_shape_no_secret(monkeypatch):
    from types import SimpleNamespace as N
    from app.features.assessment import azure_pg, gcp_sql, pg_connect, rds
    monkeypatch.setenv("ALPHA_GCP_PROJECT", "p1")
    monkeypatch.setenv("ALPHA_AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("ALPHA_ALLOW_ENV_REFS", "1")
    monkeypatch.setenv("X_USER", "u")
    monkeypatch.setenv("X_PASSWORD", "pw-secret")
    monkeypatch.setattr(rds, "collect_rds", lambda *a, **k: rds.RdsResult())
    gcp = {"name": "pg1", "databaseVersion": "POSTGRES_15", "connectionName": "p:r:pg1",
           "ipAddresses": [{"type": "PRIMARY", "ipAddress": "1.1.1.1"}], "settings": {"ipConfiguration": {"ipv4Enabled": True}}}
    c = N(execute=lambda: gcp)
    monkeypatch.setattr(gcp_sql, "_client", lambda s: N(instances=lambda: N(get=lambda **k: c)))
    monkeypatch.setattr(gcp_sql, "proxy_tunnel", lambda conn, private: (lambda: (_ for _ in ()).throw(RuntimeError("pw-secret"))))
    srv = N(version="16", sku=N(name="s"), storage=N(storage_size_gb=1), fully_qualified_domain_name="h",
            network=N(public_network_access="Enabled", delegated_subnet_resource_id=None))
    az = N(servers=N(get=lambda rg, n: srv), configurations=N(get=lambda *a: N(value="logical")),
           firewall_rules=N(list_by_server=lambda *a: [1]))
    monkeypatch.setattr(azure_pg, "_client", lambda s, sub: az)
    monkeypatch.setattr(pg_connect, "collect_catalog", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pw-secret")))
    keys = None
    for cloud, rid in [("aws", "i"), ("gcp", "pg1"), ("azure", "rg/srv")]:
        out = db.collect(AssessmentInput(source_cloud=cloud, rds_instance_id=rid, db_admin_ref="env://X", **KW), object())
        d = out.model_dump()
        assert set(d) == {"facts", "source_db", "network", "gaps"}
        keys = keys or {k: set(v) for k, v in d.items() if k != "gaps"}
        assert {k: set(v) for k, v in d.items() if k != "gaps"} == keys
        assert "pw-secret" not in str(d)


def test_unknown_csp_gap_not_raise():
    out = db.collect(AssessmentInput.model_construct(source_cloud="oracle", **KW), object())
    assert out.gaps == ["지원하지 않는 원본 클라우드: oracle"]


def test_replication_role_per_csp():
    assert "'rds_replication'" in pg_tables.sql_can_replicate("aws")
    for cloud in ("aws", "gcp", "azure"):
        assert "rds_superuser" not in pg_tables.sql_can_replicate(cloud)
    for cloud in ("gcp", "azure"):  # 관리자 역할은 REPLICATION 을 주지 않으므로 속성만 본다
        sql = pg_tables.sql_can_replicate(cloud)
        assert "pg_auth_members" not in sql and "rolreplication" in sql


def test_logical_flag_per_csp():
    assert logical_enabled("aws", "1") and not logical_enabled("aws", "0")
    assert logical_enabled("gcp", "on") and not logical_enabled("gcp", "off")
    assert logical_enabled("azure", "logical") and not logical_enabled("azure", "replica")
    assert logical_enabled("gcp", None) is None
