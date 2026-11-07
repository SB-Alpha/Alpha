"""GCP·Azure 원본 자동 조회 (cloud_source.py). 클라우드 API 응답은 공식 문서의 모양을 따른 가짜입니다."""

import pytest
from pydantic import ValidationError

from app.features.assessment import cloud_source, service
from app.features.assessment.schemas import AssessmentInput

CATALOG = {
    "databases": ["postgres", "guestbook"], "current_database": "guestbook", "wal_level": "logical",
    "max_replication_slots": 10, "slots_in_use": 0, "can_replicate": True, "gaps": [],
    "tables": [{"schema": "public", "name": "entries", "qualified": "public.entries", "has_primary_key": True,
                "primary_key_columns": ["id"], "replica_identity": "default", "row_estimate": 3, "owner": "app"}],
}

CLOUD_RUN = {
    "template": {"containers": [{
        "image": "asia-northeast3-docker.pkg.dev/p1/repo/guestbook@sha256:abc",
        "ports": [{"containerPort": 8000}],
        "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
        "startupProbe": {"httpGet": {"path": "/health"}},
        "env": [
            {"name": "PGUSER", "value": "guestbook"},
            {"name": "PGDATABASE", "value": "guestbook"},
            {"name": "LOG_LEVEL", "value": "INFO"},
            {"name": "API_TOKEN", "value": "plain-secret"},
            {"name": "PGPASSWORD", "valueSource": {"secretKeyRef": {"secret": "guestbook-db-password", "version": "1"}}},
        ],
    }]},
}
CLOUD_SQL = {
    "databaseVersion": "POSTGRES_16",
    "settings": {"tier": "db-custom-2-7680", "dataDiskSizeGb": "20",
                 "ipConfiguration": {"ipv4Enabled": True, "privateNetwork": "projects/p1/global/networks/vpc"},
                 "databaseFlags": [{"name": "cloudsql.logical_decoding", "value": "on"}]},
    "ipAddresses": [{"type": "PRIMARY", "ipAddress": "34.1.2.3"}, {"type": "PRIVATE", "ipAddress": "10.0.0.3"}],
}
CLOUD_SQL_DBS = {"items": [{"name": "postgres"}, {"name": "cloudsqladmin"}, {"name": "guestbook"}]}

CONTAINER_APP = {"properties": {
    "configuration": {"ingress": {"targetPort": 8080, "transport": "auto"}},
    "template": {"containers": [{
        "image": "alphaacr.azurecr.io/guestbook:1.0",
        "resources": {"cpu": 0.5, "memory": "1Gi"},
        "probes": [{"type": "Liveness", "httpGet": {"path": "/health/live", "port": 8080}}],
        "env": [{"name": "PGUSER", "value": "guestbook"}, {"name": "DB_PASSWORD", "secretRef": "db-password"}],
    }]},
}}
FLEX = {"sku": {"name": "Standard_D2s_v3", "tier": "GeneralPurpose"}, "properties": {
    "version": "16", "fullyQualifiedDomainName": "gb.postgres.database.azure.com",
    "storage": {"storageSizeGB": 32}, "network": {"publicNetworkAccess": "Enabled"},
}}


def _fake(responses: dict[str, dict], seen: list[str] | None = None):
    def get_json(url: str, headers: dict) -> dict:
        assert headers["Authorization"] == "Bearer t"
        if seen is not None:
            seen.append(url)
        for part, body in responses.items():
            if part in url:
                return body
        raise RuntimeError(f"unexpected {url}")
    return get_json


@pytest.fixture
def db_ok(monkeypatch):
    monkeypatch.setattr(cloud_source.refs, "resolve_db_admin", lambda ref: ({"user": "u", "password": "p"}, None))
    calls = []
    monkeypatch.setattr(cloud_source.pg_connect, "collect_catalog",
                        lambda host, port, user, pw, db: calls.append((host, port, db)) or CATALOG)
    return calls


def _gcp_inp(**src):
    return AssessmentInput(source_kind="gcp", target_region="asia-northeast3", db_admin_ref="secret://r/db",
                           cloud_source={"project_id": "p1", "region": "asia-northeast3", "app_name": "guestbook",
                                         "db_instance": "guestbook-db", **src})


def _azure_inp():
    return AssessmentInput(source_kind="azure", target_region="asia-northeast3", db_admin_ref="secret://r/db",
                           cloud_source={"subscription_id": "sub1", "resource_group": "rg", "region": "koreacentral",
                                         "app_name": "guestbook", "db_instance": "gb",
                                         "credentials_ref": "secret://r/azure"})


GCP_RESPONSES = {"/services/guestbook": CLOUD_RUN, "/instances/guestbook-db/databases": CLOUD_SQL_DBS,
                 "/instances/guestbook-db": CLOUD_SQL}


def test_gcp_reads_cloud_run_and_cloud_sql(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    seen: list[str] = []
    db, app, region = cloud_source.collect_gcp(_gcp_inp(), _fake(GCP_RESPONSES, seen))

    assert region == "asia-northeast3" and app.account_id == "p1"
    assert app.facts.image_uri.endswith("@sha256:abc")
    assert (app.facts.container_port, app.facts.cpu_units, app.facts.memory_mib) == (8000, 1024, 512)
    assert app.facts.health_check_path == "/health"
    assert app.facts.env_vars == {"PGUSER": "guestbook", "PGDATABASE": "guestbook", "LOG_LEVEL": "INFO"}
    assert app.facts.secret_refs == {"PGPASSWORD": "guestbook-db-password:1"}
    assert app.facts.container_settings == []
    assert any("API_TOKEN" in g for g in app.gaps)

    assert db.facts.engine == "postgres" and db.facts.engine_version == "16"
    assert (db.facts.vcpu, db.facts.memory_gib, db.facts.allocated_storage_gb) == (2, 7.5, 20)
    assert db.facts.logical_replication is True and db.facts.publicly_accessible is True
    assert db.facts.database_name == "guestbook" and [t.name for t in db.facts.tables] == ["entries"]
    assert db.source_db.host == "34.1.2.3" and db_ok == [("34.1.2.3", 5432, "guestbook")]
    assert db.network.vpc_id == "projects/p1/global/networks/vpc"
    assert all(u.startswith(("https://run.googleapis.com/", "https://sqladmin.googleapis.com/")) for u in seen)
    assert db.gaps == []


def test_gcp_private_only_db_uses_private_ip_and_unknown_tier_is_not_invented(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    sql = {**CLOUD_SQL, "settings": {**CLOUD_SQL["settings"], "tier": "db-f1-micro",
                                     "ipConfiguration": {"ipv4Enabled": False}}}
    db, _, _ = cloud_source.collect_gcp(_gcp_inp(), _fake({**GCP_RESPONSES, "/instances/guestbook-db": sql}))
    assert db.source_db.host == "10.0.0.3" and db.facts.publicly_accessible is False
    assert db.facts.vcpu is None and db.facts.memory_gib is None and db.facts.instance_class == "db-f1-micro"


def test_gcp_unreachable_db_is_a_gap_not_a_crash(monkeypatch):
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    monkeypatch.setattr(cloud_source.refs, "resolve_db_admin", lambda ref: ({"user": "u", "password": "p"}, None))

    def refuse(*args):
        raise TimeoutError("password=p")  # 메시지는 gaps 로 새면 안 됨

    monkeypatch.setattr(cloud_source.pg_connect, "collect_catalog", refuse)
    db, _, _ = cloud_source.collect_gcp(_gcp_inp(), _fake(GCP_RESPONSES))
    assert db.facts.tables == []
    assert any("DB 접속 실패 (TimeoutError)" in g for g in db.gaps)
    assert not any("password" in g for g in db.gaps)


def test_gcp_db_name_is_not_guessed_when_several_user_dbs(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    run = {"template": {"containers": [{"image": "i", "ports": [{"containerPort": 80}], "env": []}]}}
    dbs = {"items": [{"name": "postgres"}, {"name": "a"}, {"name": "b"}]}
    db, _, _ = cloud_source.collect_gcp(_gcp_inp(), _fake({"/services/guestbook": run,
                                                           "/instances/guestbook-db/databases": dbs,
                                                           "/instances/guestbook-db": CLOUD_SQL}))
    assert db.facts.database_name is None and db_ok == []
    assert any("후보: a, b" in g for g in db.gaps)


def test_azure_reads_container_app_and_flexible_server(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "azure_token", lambda ref: "t")
    seen: list[str] = []
    responses = {"/containerApps/guestbook": CONTAINER_APP, "/configurations/wal_level": {"properties": {"value": "logical"}},
                 "/flexibleServers/gb/databases": {"value": [{"name": "azure_maintenance"}, {"name": "azure_sys"},
                                                              {"name": "postgres"}, {"name": "guestbook"}]},
                 "/flexibleServers/gb?": FLEX}
    db, app, region = cloud_source.collect_azure(_azure_inp(), _fake(responses, seen))

    assert region == "koreacentral" and app.account_id == "sub1"
    assert (app.facts.container_port, app.facts.cpu_units, app.facts.memory_mib) == (8080, 512, 1024)
    assert app.facts.health_check_path == "/health/live"
    assert app.facts.secret_refs == {"DB_PASSWORD": "containerapp-secret:db-password"}
    assert app.facts.container_settings == []
    assert db.facts.engine_version == "16" and db.facts.vcpu == 2 and db.facts.memory_gib is None
    assert db.facts.logical_replication is True and db.facts.allocated_storage_gb == 32
    assert db.source_db.host == "gb.postgres.database.azure.com" and db.facts.database_name == "guestbook"
    assert [t.name for t in db.facts.tables] == ["entries"]
    assert all("/subscriptions/sub1/resourceGroups/rg/providers/" in u for u in seen)


def test_azure_tcp_ingress_is_recorded_as_a_setting(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "azure_token", lambda ref: "t")
    app = {"properties": {**CONTAINER_APP["properties"],
                          "configuration": {"ingress": {"targetPort": 5000, "transport": "tcp"}}}}
    _, res, _ = cloud_source.collect_azure(_azure_inp(), _fake({"/containerApps/guestbook": app,
                                                                 "/flexibleServers/gb": {}}))
    assert res.facts.container_settings == ["tcp_ingress"]


def test_service_turns_auth_failure_into_gap(monkeypatch):
    def no_login(ref):
        raise PermissionError("secret=abc")

    monkeypatch.setattr(cloud_source, "gcp_token", no_login)
    out = service.run(_gcp_inp(credentials_ref="secret://r/gcp"))
    assert out.plan.approvable is False
    assert any("원본 GCP 인증·조회 실패 (PermissionError)" in g for g in out.gaps)
    assert not any("abc" in g for g in out.gaps)
    assert out.source.credentials_ref.cloud == "secret://r/gcp"


def test_service_gcp_output_has_same_shape(monkeypatch, db_ok):
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    monkeypatch.setattr(cloud_source, "_get_json", _fake(GCP_RESPONSES))
    out = service.run(_gcp_inp())
    assert out.source.account_id == "p1" and out.source.region == "asia-northeast3"
    assert out.source.db.host == "34.1.2.3" and out.source.tables == ["public.entries"]
    assert out.source.app.db_user == "guestbook"
    assert out.plan.target_infra.db_name == "guestbook"


@pytest.mark.parametrize("kind,src,missing", [
    ("gcp", {"region": "r", "app_name": "a", "db_instance": "d"}, "project_id"),
    ("azure", {"region": "r", "app_name": "a", "db_instance": "d", "subscription_id": "s"}, "resource_group"),
    ("azure", {"region": "r", "app_name": "a", "db_instance": "d", "subscription_id": "s", "resource_group": "g"},
     "credentials_ref"),
])
def test_cloud_source_requires_its_values(kind, src, missing):
    with pytest.raises(ValidationError, match=missing):
        AssessmentInput(source_kind=kind, target_region="asia-northeast3", cloud_source=src)
    with pytest.raises(ValidationError, match="cloud_source"):
        AssessmentInput(source_kind=kind, target_region="asia-northeast3")


@pytest.mark.parametrize("value,mib", [("512Mi", 512), ("1Gi", 1024), ("1.5Gi", 1536), ("2G", 2048), ("x", None)])
def test_memory_units(value, mib):
    assert cloud_source._memory_mib(value) == mib


@pytest.mark.parametrize("value,units", [("1", 1024), ("1000m", 1024), (0.5, 512), ("2", 2048), ("", None)])
def test_cpu_units(value, units):
    assert cloud_source._cpu_units(value) == units


def test_workflows_request_passes_cloud_source_to_assessment(monkeypatch):
    from app.features.workflows import runners
    from app.features.workflows.schemas import MigrationRequest

    with pytest.raises(ValidationError, match="cloud_source"):
        MigrationRequest(source_kind="gcp", target_region="asia-northeast3")
    req = MigrationRequest(source_kind="gcp", target_region="asia-northeast3", db_admin_ref="secret://r/db",
                           cloud_source={"project_id": "p1", "region": "asia-northeast3", "app_name": "guestbook",
                                         "db_instance": "guestbook-db"})
    got = {}
    monkeypatch.setattr(service, "run", lambda inp, ctx=None: got.setdefault("inp", inp))

    class Run:
        run_id, request = "mig-1", req

    runners.run_assessment(Run(), None)
    assert got["inp"].source_kind == "gcp" and got["inp"].cloud_source.app_name == "guestbook"
    assert got["inp"].db_admin_ref == "secret://r/db"


# --- 자격 증명 이름표와 주소 경로 검증 (리뷰 반영) ---------------------------------------------------------


def _fake_google_auth(monkeypatch):
    """google.auth 를 가짜로 바꿔 ADC 가 불렸는지 봅니다. 토큰 발급은 실제로 하지 않습니다."""
    import sys
    import types

    used = []
    ga = types.ModuleType("google.auth")
    gat = types.ModuleType("google.auth.transport")
    gatu = types.ModuleType("google.auth.transport.urllib3")
    ga.default = lambda scopes=None: (types.SimpleNamespace(token="adc-token", refresh=lambda r: used.append("adc")), "p")
    gatu.Request = lambda pm: None
    ga.transport, gat.urllib3 = gat, gatu
    g = types.ModuleType("google")
    g.auth = ga
    ul = types.ModuleType("urllib3")
    ul.PoolManager = lambda: None
    for name, mod in {"google": g, "google.auth": ga, "google.auth.transport": gat,
                      "google.auth.transport.urllib3": gatu, "urllib3": ul}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return used


@pytest.mark.parametrize("ref", [None, ""])
def test_gcp_blank_credentials_ref_uses_adc(monkeypatch, ref):
    used = _fake_google_auth(monkeypatch)
    assert cloud_source.gcp_token(ref) == "adc-token" and used == ["adc"]


@pytest.mark.parametrize("ref", ["gcp-key", "arn:aws:secretsmanager:x", "file:///tmp/key.json"])
def test_gcp_wrong_shaped_credentials_ref_never_falls_back_to_adc(monkeypatch, ref):
    used = _fake_google_auth(monkeypatch)
    with pytest.raises(ValueError):
        cloud_source.gcp_token(ref)
    assert used == []  # 서버 로그인으로 조용히 넘어가지 않음


@pytest.mark.parametrize("kind", ["gcp", "azure"])
def test_cloud_source_rejects_wrong_shaped_credentials_ref_at_the_door(kind):
    src = {"region": "r", "app_name": "a", "db_instance": "d", "credentials_ref": "gcp-key",
           "project_id": "p", "subscription_id": "s", "resource_group": "g"}
    with pytest.raises(ValidationError, match="secret://"):
        AssessmentInput(source_kind=kind, target_region="asia-northeast3", cloud_source=src)


def test_cloud_source_blank_credentials_ref_is_none_for_gcp():
    inp = AssessmentInput(source_kind="gcp", target_region="asia-northeast3", cloud_source={
        "region": "r", "app_name": "a", "db_instance": "d", "project_id": "p", "credentials_ref": ""})
    assert inp.cloud_source.credentials_ref is None


@pytest.mark.parametrize("field,value", [
    ("app_name", "a/../b"), ("app_name", "x?alt=media"), ("db_instance", "d#f"), ("region", "r%2F"),
    ("project_id", "p q"), ("app_name", ".."), ("app_name", ""),
])
def test_cloud_source_rejects_names_that_would_change_the_api_path(field, value):
    src = {"region": "r", "app_name": "a", "db_instance": "d", "project_id": "p", field: value}
    with pytest.raises(ValidationError):
        AssessmentInput(source_kind="gcp", target_region="asia-northeast3", cloud_source=src)


def test_url_segments_are_escaped_even_when_called_directly(monkeypatch):
    seen: list[str] = []
    inp = _gcp_inp()
    object.__setattr__(inp.cloud_source, "app_name", "a/../b")  # 스키마 검증을 건너뛴 직접 호출 상황
    monkeypatch.setattr(cloud_source, "gcp_token", lambda ref: "t")
    cloud_source.collect_gcp(inp, _fake({"/services/": CLOUD_RUN, "/databases": CLOUD_SQL_DBS, "/instances/": CLOUD_SQL}, seen))
    assert any("/services/a%2F..%2Fb" in u for u in seen) and not any("/services/a/../b" in u for u in seen)
