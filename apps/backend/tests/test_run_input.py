"""The run entrance: input shape, stored credentials, and no secret value leaving it."""

import copy
import threading
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.secrets import has_secret, put_secret, resolve
from app.features.db_migration import cloud_lookup, db_workflow, run_input
from app.features.db_migration import router as db_router
from app.features.db_migration.client import AgentError
from app.features.db_migration.cloud_guides import CLOUD_GUIDES, credential_next_steps
from app.main import app

SOURCE_PASSWORD = "source-pass-value"
TARGET_PASSWORD = "target-pass-value"
INPUT = {
    "run_id": "mig-001",
    "source": {
        "cloud": "aws",
        "region": "ap-northeast-2",
        "db": {"user": "admin", "password": SOURCE_PASSWORD},
    },
    "target": {
        "cloud": "gcp",
        "region": "asia-northeast3",
        "db": {"user": "postgres", "password": TARGET_PASSWORD, "app_user": "alpha"},
    },
}
CREDENTIALS = {
    "aws": {"aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "aws-secret-value"},
    "gcp": {"credentials_json": "gcp-secret-value"},
    "azure": {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "azure-secret-value",
        "subscription_id": "subscription",
    },
}


class NoJobAgent:
    def get_job(self, _job_id: str) -> dict:
        raise db_workflow.AgentError(404, "not found")

    def abort(self, _job_id: str) -> dict:
        raise db_workflow.AgentError(404, "not found")


def _wait_for_lookup(run_id):
    deadline = time.monotonic() + 5
    while run_id in db_workflow._active and time.monotonic() < deadline:
        threading.Event().wait(0.005)
    assert run_id not in db_workflow._active


def _run_and_wait(*args, **kwargs):
    run_id = db_workflow.run(*args, **kwargs)
    _wait_for_lookup(run_id)
    return run_id


def _found_route(injected):
    def endpoint(side):
        return SimpleNamespace(cloud=side.cloud, region=side.region,
                               scope_id="scope", network={},
                               db=SimpleNamespace(instance="db", host="10.20.0.5", port=5432, version=None,
                                                  publicly_accessible=False, logical_replication=None),
                               cidrs=("10.20.0.0/24",))
    return SimpleNamespace(source=endpoint(injected.source), target=endpoint(injected.target))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_lookup, "find_route", _found_route)
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    with db_workflow._active_lock:
        db_workflow._active.clear()
        db_workflow._runs.clear()


@pytest.fixture
def credentials():
    for cloud, value in CREDENTIALS.items():
        put_secret(f"cloud/{cloud}", value)


def changed(path: str, value):
    data = copy.deepcopy(INPUT)
    *parents, last = path.split(".")
    node = data
    for name in parents:
        node = node[name]
    node[last] = value
    return data


def fields_of(raw) -> list[str]:
    with pytest.raises(run_input.InvalidInput) as caught:
        run_input.parse(raw)
    return caught.value.fields


def test_inject_returns_references_and_stores_accounts(credentials):
    injected = run_input.inject("mig-001", run_input.parse(INPUT))

    assert injected.source.credentials_ref == "secret://cloud/aws"
    assert injected.target.credentials_ref == "secret://cloud/gcp"
    assert injected.target_app_user == "alpha"
    assert resolve(injected.source.db_admin_ref) == {"user": "admin", "password": SOURCE_PASSWORD}
    assert resolve(injected.target.db_admin_ref) == {"user": "postgres", "password": TARGET_PASSWORD}
    assert SOURCE_PASSWORD not in repr(injected) and TARGET_PASSWORD not in repr(injected)


def test_azure_target_uses_azure_credentials(credentials):
    raw = changed("target.cloud", "azure")
    raw["target"]["region"] = "koreacentral"

    assert run_input.inject("mig-001", run_input.parse(raw)).target.credentials_ref == "secret://cloud/azure"


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        ("source.cloud", "oracle", "source.cloud"),
        ("target.cloud", "oracle", "target.cloud"),
        ("source.region", "asia-northeast3", "source.region"),
        ("target.region", "ap-northeast-2", "target.region"),
        ("run_id", "Bad_Id", "run_id"),
        ("source.db.password", "", "source.db.password"),
        ("target.db.app_user", " alpha", "target.db.app_user"),
    ],
)
def test_bad_values_are_named_by_field(path, value, field):
    assert fields_of(changed(path, value)) == [field]


def test_run_id_is_optional():
    raw = copy.deepcopy(INPUT)
    del raw["run_id"]

    assert run_input.parse(raw).run_id is None


def test_missing_target_app_user_is_refused():
    raw = copy.deepcopy(INPUT)
    del raw["target"]["db"]["app_user"]

    assert fields_of(raw) == ["target.db.app_user"]


def test_error_never_quotes_a_value_even_in_an_unknown_key():
    pasted = "pasted-secret-value"
    raw = changed("source.db.password", 123)
    raw["source"][pasted] = pasted

    with pytest.raises(run_input.InvalidInput) as caught:
        run_input.parse(raw)

    assert caught.value.fields == ["source.db.password", "source.unknown_field"]
    assert pasted not in str(caught.value)


def test_non_object_input_is_refused():
    assert fields_of(["not", "an", "object"]) == ["input"]


def test_unregistered_credentials_refuse_before_saving_accounts():
    put_secret("cloud/aws", CREDENTIALS["aws"])

    with pytest.raises(run_input.MissingCredentials, match="gcp"):
        run_input.inject("mig-001", run_input.parse(INPUT))

    assert not has_secret("secret://mig-001/source-db-admin")


def test_credentials_with_wrong_keys_do_not_leak_them():
    put_secret("cloud/aws", CREDENTIALS["aws"])
    put_secret("cloud/gcp", {"oops-pasted-key": "gcp-secret-value"})

    with pytest.raises(run_input.MissingCredentials) as caught:
        run_input.inject("mig-001", run_input.parse(INPUT))

    assert "credentials_json" in str(caught.value)
    assert "oops-pasted-key" not in str(caught.value) and "gcp-secret-value" not in str(caught.value)


def test_run_that_fails_before_the_vpn_keeps_its_inputs_for_a_retry(credentials):
    agent = NoJobAgent()

    assert _run_and_wait(INPUT, agent=agent) == "mig-001"

    status = db_workflow.status("mig-001", agent=agent)
    assert status.state == "failed"
    assert status.cleanup_required
    assert {"secret:source-db-admin", "secret:target-admin"} <= set(status.remaining)
    assert "mig-001" not in db_workflow._active


def test_run_makes_a_run_id_when_left_out(credentials):
    raw = copy.deepcopy(INPUT)
    del raw["run_id"]

    assert _run_and_wait(raw, agent=NoJobAgent()).startswith("mig-")


def test_run_without_credentials_leaves_no_run_behind():
    agent = NoJobAgent()

    with pytest.raises(run_input.MissingCredentials):
        _run_and_wait(INPUT, agent=agent)

    with pytest.raises(db_workflow.UnknownRun):
        db_workflow.status("mig-001", agent=agent)
    assert "mig-001" not in db_workflow._active


def test_run_refuses_a_run_that_is_already_active(credentials):
    with db_workflow._active_lock:
        db_workflow._active.add("mig-001")

    with pytest.raises(db_workflow.NotAllowed):
        _run_and_wait(INPUT, agent=NoJobAgent())


def test_stop_removes_the_saved_accounts(credentials):
    agent = NoJobAgent()
    _run_and_wait(INPUT, agent=agent)

    result = db_workflow.stop("mig-001", agent=agent)

    assert not result.remaining
    assert not has_secret("secret://mig-001/source-db-admin")
    assert not has_secret("secret://mig-001/target-admin")
    assert has_secret("secret://cloud/aws")


@pytest.fixture
def http():
    app.dependency_overrides[db_router.get_client] = NoJobAgent
    yield TestClient(app)
    app.dependency_overrides.pop(db_router.get_client, None)


def test_http_accepts_the_input(http, credentials):
    resp = http.post("/db-migration/runs", json=INPUT)

    assert resp.status_code == 202 and resp.json() == {"run_id": "mig-001"}
    _wait_for_lookup("mig-001")
    assert http.get("/db-migration/runs/mig-001").json()["state"] == "failed"


def test_http_bad_input_names_the_field_and_quotes_no_value(http):
    resp = http.post("/db-migration/runs", json=changed("source.region", SOURCE_PASSWORD))

    assert resp.status_code == 422 and resp.json()["fields"] == ["source.region"]
    assert SOURCE_PASSWORD not in resp.text


def test_http_missing_credentials_is_a_conflict(http):
    resp = http.post("/db-migration/runs", json=INPUT)

    assert resp.status_code == 409 and "aws" in resp.json()["detail"]
    assert SOURCE_PASSWORD not in resp.text


def test_cleanup_guidance_follows_the_clouds_of_the_input(credentials):
    agent = NoJobAgent()
    raw = changed("target.cloud", "azure")
    raw["target"]["region"] = "koreacentral"
    _run_and_wait(raw, agent=agent)

    steps = db_workflow.stop("mig-001", agent=agent).next_steps

    assert [step.split("에서")[0] for step in steps] == ["AWS 콘솔", "Azure Portal"]
    assert "IAM 사용자" in steps[0] and "서비스 주체" in steps[1]
    assert not any("Google Cloud" in step or "이미 지웠습니다" in step for step in steps)


def test_cleanup_guidance_for_gcp_target(credentials):
    agent = NoJobAgent()
    _run_and_wait(INPUT, agent=agent)

    steps = db_workflow.stop("mig-001", agent=agent).next_steps

    assert [step.split("에서")[0] for step in steps] == ["AWS 콘솔", "Google Cloud 콘솔"]


def test_every_supported_cloud_has_a_guide():
    assert CLOUD_GUIDES.keys() == run_input.CLOUD_CREDENTIAL_KEYS.keys()


def test_same_cloud_is_guided_once():
    assert len(credential_next_steps("aws", "aws")) == 1


def test_run_started_elsewhere_keeps_the_previous_guidance():
    assert db_workflow._credential_steps("from-workflows") == [db_workflow.IAM_NEXT_STEP]


REGIONS = {"aws": "ap-northeast-2", "gcp": "asia-northeast3", "azure": "koreacentral"}
CONSOLES = {"aws": "AWS 콘솔", "gcp": "Google Cloud 콘솔", "azure": "Azure Portal"}
ROUTES = [
    ("aws", "gcp"),
    ("aws", "azure"),
    ("gcp", "aws"),
    ("gcp", "azure"),
    ("azure", "aws"),
    ("azure", "gcp"),
]


def route_input(source: str, target: str) -> dict:
    raw = copy.deepcopy(INPUT)
    raw["source"].update(cloud=source, region=REGIONS[source])
    raw["target"].update(cloud=target, region=REGIONS[target])
    return raw


@pytest.mark.parametrize(("source", "target"), ROUTES)
def test_every_route_is_injected_the_same_way(credentials, source, target):
    injected = run_input.inject("mig-001", run_input.parse(route_input(source, target)))

    assert injected.source.credentials_ref == f"secret://cloud/{source}"
    assert injected.target.credentials_ref == f"secret://cloud/{target}"
    assert resolve(injected.source.db_admin_ref)["password"] == SOURCE_PASSWORD
    assert resolve(injected.target.db_admin_ref)["password"] == TARGET_PASSWORD


@pytest.mark.parametrize(("source", "target"), ROUTES)
def test_every_route_gets_guidance_for_its_own_clouds(credentials, source, target):
    agent = NoJobAgent()
    _run_and_wait(route_input(source, target), agent=agent)

    steps = db_workflow.stop("mig-001", agent=agent).next_steps

    assert [step.split("에서")[0] for step in steps] == [CONSOLES[source], CONSOLES[target]]


@pytest.mark.parametrize("cloud", sorted(REGIONS))
def test_same_cloud_is_refused(cloud):
    assert fields_of(route_input(cloud, cloud)) == ["target.cloud"]


def test_route_needs_the_credentials_of_both_clouds():
    put_secret("cloud/gcp", CREDENTIALS["gcp"])

    with pytest.raises(run_input.MissingCredentials, match="azure"):
        run_input.inject("mig-001", run_input.parse(route_input("gcp", "azure")))


class _NoJobAgent:
    def get_job(self, job_id):
        raise AgentError(404, "no such job")


@pytest.mark.parametrize(
    ("source", "target"),
    [("aws", "gcp"), ("aws", "azure"), ("gcp", "aws"), ("gcp", "azure"), ("azure", "aws"), ("azure", "gcp")],
)
def test_status_gives_the_route_in_the_same_form_on_all_six_routes(source, target):
    for cloud in (source, target):
        put_secret(f"cloud/{cloud}", CREDENTIALS[cloud])
    regions = {"aws": "ap-northeast-2", "gcp": "asia-northeast3", "azure": "koreacentral"}
    body = copy.deepcopy(INPUT)
    body["source"].update(cloud=source, region=regions[source])
    body["target"].update(cloud=target, region=regions[target])

    agent = _NoJobAgent()
    run_id = _run_and_wait(body, agent=agent)
    status = db_workflow.status(run_id, agent=agent)

    assert status.route.model_dump() == {
        "source": {"cloud": source, "region": regions[source]},
        "target": {"cloud": target, "region": regions[target]},
    }
    assert SOURCE_PASSWORD not in status.model_dump_json() and TARGET_PASSWORD not in status.model_dump_json()
