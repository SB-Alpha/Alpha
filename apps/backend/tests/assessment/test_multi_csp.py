"""source_kind 분기: 기본값, stub CSP 의 gaps, CSP별 복제 역할·논리 복제 플래그."""

import pytest

from app.features.assessment import db, pg_tables
from app.features.assessment.csp import logical_enabled
from app.features.assessment.schemas import AssessmentInput

KW = dict(aws_credentials_ref="profile:x", aws_region="ap-northeast-2", target_region="asia-northeast3")
# gcp·azure 는 cloud_source 가 필수(#94 검증). 중계 수집기는 읽지 않는다
CS = {"gcp": dict(region="asia-northeast3", app_name="a", db_instance="pg1", project_id="p"),
      "azure": dict(region="koreacentral", app_name="a", db_instance="s", subscription_id="sub",
                    resource_group="rg", credentials_ref="secret://x"), "aws": None}


def test_source_kind_default_aws():
    assert AssessmentInput(**KW).source_kind == "aws"


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_csp_without_collector_falls_to_gaps(cloud, monkeypatch):
    monkeypatch.delitem(db.COLLECTORS, cloud)
    out = db.collect(AssessmentInput(source_kind=cloud, db_instance_id="x", cloud_source=CS[cloud], **KW), object())
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
        out = db.collect(AssessmentInput(source_kind=cloud, db_instance_id=rid, cloud_source=CS[cloud], db_admin_ref="env://X", **KW), object())
        d = out.model_dump()
        assert set(d) == {"facts", "source_db", "network", "region", "account_id", "gaps"}
        keys = keys or {k: set(v) for k, v in d.items() if k not in ("gaps", "region", "account_id")}
        assert {k: set(v) for k, v in d.items() if k not in ("gaps", "region", "account_id")} == keys
        assert "pw-secret" not in str(d)


def test_unknown_csp_gap_not_raise():
    out = db.collect(AssessmentInput.model_construct(source_kind="oracle", **KW), object())
    assert out.gaps == ["지원하지 않는 원본 클라우드: oracle"]


def test_replication_role_per_csp():
    aws = pg_tables.sql_can_replicate("aws")  # 상속 멤버십까지(pg_has_role), 직접 멤버(pg_auth_members) 아님
    assert "'rds_replication'" in aws and "pg_has_role" in aws and "pg_auth_members" not in aws
    for cloud in ("aws", "gcp", "azure"):
        assert "rds_superuser" not in pg_tables.sql_can_replicate(cloud)
    for cloud in ("gcp", "azure"):  # 관리자 역할은 REPLICATION 을 주지 않으므로 속성만 본다
        sql = pg_tables.sql_can_replicate(cloud)
        assert "pg_has_role" not in sql and "pg_auth_members" not in sql and "rolreplication" in sql


def test_logical_flag_per_csp():
    assert logical_enabled("aws", "1") and not logical_enabled("aws", "0")
    assert logical_enabled("gcp", "on") and not logical_enabled("gcp", "off")
    assert logical_enabled("azure", "logical") and not logical_enabled("azure", "replica")
    assert logical_enabled("gcp", None) is None


def test_gcp_azure_need_neither_aws_nor_manual():
    for k in ("gcp", "azure"):  # AWS·manual 값은 필요 없고 cloud_source 가 필수(#94). db_instance_id 만으로는 통과하지 않는다
        assert AssessmentInput(source_kind=k, cloud_source=CS[k], target_region="t").source_kind == k
        with pytest.raises(ValueError):
            AssessmentInput(source_kind=k, db_instance_id="x", target_region="t")
    with pytest.raises(ValueError):
        AssessmentInput(source_kind="manual", target_region="t")


def test_cloud_modules_import_without_sdks():
    import subprocess, sys
    code = ("import sys\nfor m in ('azure','google','googleapiclient'): sys.modules[m] = None\n"
            "import app.features.assessment.gcp_sql, app.features.assessment.azure_pg, app.features.assessment.db")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0
