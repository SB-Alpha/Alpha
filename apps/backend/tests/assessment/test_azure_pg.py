from types import SimpleNamespace as N

import pytest

from app.features.assessment import azure_pg, db
from app.features.assessment.schemas import AssessmentInput

KW = dict(aws_credentials_ref="x", aws_region="r", target_region="t", source_cloud="azure")


class Err(Exception):
    def __init__(self, status=None, code=None):
        self.status_code = status
        self.error = N(code=code) if code else None


def server(public="Enabled", subnet=None):
    return N(version="16", sku=N(name="Standard_D2s_v3", tier="GeneralPurpose"), storage=N(storage_size_gb=128),
             network=N(public_network_access=public, delegated_subnet_resource_id=subnet),
             fully_qualified_domain_name="s.postgres.database.azure.com")


def factory(srv=None, flag="logical", rules=(1,), get_err=None, cfg_err=None):
    def cfg(rg, n, k):
        assert k == "azure.replication_support"
        if cfg_err:
            raise cfg_err
        return N(value=flag)

    def get(rg, n):
        if get_err:
            raise get_err
        return srv or server()

    c = N(servers=N(get=get), configurations=N(get=cfg), firewall_rules=N(list_by_server=lambda rg, n: list(rules)))
    return lambda session, sub: c


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("ALPHA_ALLOW_ENV_REFS", "1")
    monkeypatch.setenv("AZ_USER", "u")
    monkeypatch.setenv("AZ_PASSWORD", "pw-secret")


def run(f, rid="rg/srv", ref="env://AZ"):
    return azure_pg.collect(AssessmentInput(rds_instance_id=rid, db_admin_ref=ref, **KW), None, f)


def test_ok():
    r = run(factory())
    assert r.gaps == []
    assert (r.db.engine_version, r.db.instance_class, r.db.allocated_storage_gb) == ("16", "Standard_D2s_v3", 128)
    assert r.db.logical_replication is True and r.source_db.instance_id == "rg/srv"
    assert r.creds == {"user": "u", "password": "pw-secret"}
    with r.tunnel() as hp:
        assert hp == ("s.postgres.database.azure.com", 5432)


def test_flag_off():
    assert run(factory(flag="replica")).db.logical_replication is False


def test_denied_on_config_keeps_none():
    r = run(factory(cfg_err=Err(403, "AuthorizationFailed")))
    assert r.db.logical_replication is None and any("AuthorizationFailed" in g for g in r.gaps)


def test_denied_on_server():
    r = run(factory(get_err=Err(403, "AuthorizationFailed")))
    assert r.source_db.instance_id is None and any("AuthorizationFailed" in g for g in r.gaps)


def test_not_found():
    r = run(factory(get_err=Err(404)))
    assert r.source_db.instance_id is None and any("찾지 못함" in g for g in r.gaps)


def test_no_subscription(monkeypatch):
    monkeypatch.delenv("ALPHA_AZURE_SUBSCRIPTION_ID")
    assert any("구독" in g for g in run(factory()).gaps)


@pytest.mark.parametrize("rid", [None, "srv", "a/b/c", "rg/Bad_Name"])
def test_bad_id(rid):
    r = run(factory(), rid=rid)
    assert any("형식" in g for g in r.gaps) and r.source_db.instance_id is None


def test_private_no_tunnel():
    r = run(factory(srv=server(public="Disabled", subnet="/subnets/x")))
    assert r.tunnel is None and r.network.vpc_id == "/subnets/x" and r.db.publicly_accessible is False


def test_no_firewall_rules_no_tunnel():
    assert run(factory(rules=())).tunnel is None


def test_no_creds_no_leak():
    r = run(factory(), ref=None)
    assert r.creds is None and r.tunnel is None and r.gaps


def test_integration_never_raises():
    out = db.collect(AssessmentInput(source_cloud="azure", **{k: v for k, v in KW.items() if k != "source_cloud"}), None)
    assert out.gaps
