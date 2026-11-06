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


def vm(id_, net="vpc", nat=None, ip="10.1.0.2", status="RUNNING", label="alpha-assessment-bastion", name=None):
    ac = [{"natIP": nat}] if nat else []
    return {"id": id_, "name": name or f"vm-{id_}", "zone": "https://x/zones/asia-northeast3-a", "status": status, "labels": {"purpose": label},
            "networkInterfaces": [{"network": f"https://www.googleapis.com/compute/v1/projects/p1/global/networks/{net}",
                                   "networkIP": ip, "accessConfigs": ac}]}


def fake_compute(*vms, exc=None):
    class Req:
        def execute(self):
            if exc:
                raise exc
            return {"items": {"zones/z": {"instances": list(vms)}}}

    class Inst:
        def aggregatedList(self, **kw):
            assert "alpha-assessment-bastion" in kw["filter"]
            return Req()

    class C:
        def instances(self):
            return Inst()

    return lambda session: C()


def run(client, ref="env://X", compute=None, **kw):
    tf = lambda conn, private: (lambda: (conn, private))
    sf = lambda bastion, host, cmd=None: (lambda: ("ssh", bastion, host, cmd))
    return gcp_sql.collect(AssessmentInput(db_admin_ref=ref, **KW), None, client, tf, compute or fake_compute(), sf)


def test_ok():
    s = run(fake(inst("on")))
    assert (s.db.engine, s.db.engine_version, s.db.instance_class, s.db.allocated_storage_gb) == ("postgres", "15", "db-custom-2-7680", 100)
    assert s.db.logical_replication is True and s.db.publicly_accessible is False
    assert (s.source_db.instance_id, s.source_db.host, s.source_db.port) == ("pg1", "10.0.0.5", 5432)
    assert s.network.vpc_id.endswith("/vpc")
    assert s.creds == {"user": "u", "password": "pw-secret"} and len(s.gaps) == 1 and "중계 VM" in s.gaps[0]
    assert s.tunnel() == ("p1:r:pg1", True)
    assert s.db.ha_enabled is None and s.db.backup_retention_days is None and s.network.allowed_cidrs == []


def test_ha_backup_cidrs():
    d = inst("on")
    d["settings"].update(availabilityType="REGIONAL", backupConfiguration={"backupRetentionSettings": {"retainedBackups": 14}})
    d["settings"]["ipConfiguration"]["authorizedNetworks"] = [{"value": "1.2.3.0/24"}, {"name": "x"}]
    s = run(fake(d))
    assert s.db.ha_enabled is True and s.db.backup_retention_days == 14 and s.network.allowed_cidrs == ["1.2.3.0/24"]
    d["settings"]["availabilityType"] = "ZONAL"
    assert run(fake(d)).db.ha_enabled is False


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


IAP = "gcloud compute start-iap-tunnel vm-2 22 --listen-on-stdin --project p1 --zone asia-northeast3-a"


@pytest.fixture
def gcloud(monkeypatch):
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: "/x")
    monkeypatch.delenv(gcp_sql.PROXY_ENV, raising=False)


def test_bastion_iap_same_vpc_preferred(gcloud):
    c = fake_compute(vm("1", net="other", nat="1.2.3.4"), vm("2", net="vpc"), vm("3", status="TERMINATED"), vm("4", label="x"))
    s = run(fake(inst()), compute=c)
    assert s.tunnel() == ("ssh", "vm-2", "10.0.0.5", IAP) and s.gaps == []


def test_other_vpc_fallback_used(gcloud):
    assert run(fake(inst()), compute=fake_compute(vm("1", net="other"))).tunnel()[1] == "vm-1"


def test_override_env_wins(gcloud, monkeypatch):
    monkeypatch.setenv(gcp_sql.PROXY_ENV, "custom %h %p")
    assert run(fake(inst()), compute=fake_compute(vm("2"))).tunnel()[3] == "custom %h %p"


def test_override_env_works_without_gcloud(monkeypatch):
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: None)
    monkeypatch.setenv(gcp_sql.PROXY_ENV, "custom")
    assert run(fake(inst()), compute=fake_compute(vm("2"))).tunnel()[3] == "custom"


def test_no_gcloud_falls_back_to_proxy(monkeypatch):
    monkeypatch.delenv(gcp_sql.PROXY_ENV, raising=False)
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: "/x" if b == gcp_sql.PROXY_BIN else None)
    s = run(fake(inst()), compute=fake_compute(vm("2")))
    assert s.tunnel() == ("p1:r:pg1", True) and any("gcloud" in g for g in s.gaps)


def test_iap_rejects_injection(gcloud):
    s = run(fake(inst()), compute=fake_compute(vm("2", name="a;rm -rf x")))
    assert s.tunnel() == ("p1:r:pg1", True) and any("안전하지" in g for g in s.gaps)
    with pytest.raises(ValueError):
        gcp_sql.iap_command({"name": "a", "zone": "z$(x)", "project": "p"})


def test_no_bastion_falls_back_to_proxy(monkeypatch):
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: "/x")
    s = run(fake(inst()), compute=fake_compute())
    assert s.tunnel() == ("p1:r:pg1", True) and any("중계 VM" in g for g in s.gaps)


def test_no_bastion_no_proxy_binary(monkeypatch):
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: None)
    s = gcp_sql.collect(AssessmentInput(db_admin_ref="env://X", **KW), None, fake(inst()), gcp_sql.proxy_tunnel,
                        fake_compute(), None)
    assert s.tunnel is None and any("cloud-sql-proxy" in g for g in s.gaps) and any("중계 VM" in g for g in s.gaps)


def test_compute_error_gap():
    s = run(fake(inst()), compute=fake_compute(exc=HttpError(403)))
    assert any("중계 VM 조회 실패 (HttpError 403)" in g for g in s.gaps)


def test_bad_disk_does_not_raise():
    d = inst()
    d["settings"]["dataDiskSizeGb"] = "abc"
    s = run(fake(d), compute=fake_compute(vm("1")))
    assert s.db.allocated_storage_gb is None and any("디스크" in g for g in s.gaps)


def test_proxy_missing_binary_clear_error(monkeypatch):
    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: None)
    with pytest.raises(RuntimeError, match="cloud-sql-proxy 바이너리 없음"):
        with gcp_sql.proxy_tunnel("c", False)():
            pass


def test_proxy_stops_process(monkeypatch):
    calls = []

    class P:
        pid = 0

        def poll(self):
            return None

    monkeypatch.setattr(gcp_sql.shutil, "which", lambda b: "/x")
    monkeypatch.setattr(gcp_sql.subprocess, "Popen", lambda *a, **k: P())
    monkeypatch.setattr(gcp_sql.socket, "create_connection", lambda *a, **k: type("S", (), {"close": lambda s: None})())
    monkeypatch.setattr(gcp_sql, "_stop", lambda p: calls.append(p))
    with gcp_sql.proxy_tunnel("c", False)():
        pass
    assert len(calls) == 1


def test_db_collect_never_raises():
    out = db.collect(AssessmentInput(**KW), object())  # SDK 미설치/자격 없음도 gaps
    assert out.gaps and out.source_db.instance_id is None
