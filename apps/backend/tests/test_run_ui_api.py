"""What a screen reads and calls through /db-migration. Clouds, VPN and agent are fakes."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.secrets import has_secret, resolve
from app.features.db_migration import (
    cloud_lookup,
    db_workflow,
    schemas,
    vpn,
    vpn_workflow,
)
from app.features.db_migration.router import get_client
from app.main import app

from .test_db_workflow import env, wait_until  # noqa: F401
from .test_run_flow import (  # noqa: F401
    RUN_ID,
    SOURCE_PASSWORD,
    TABLE,
    TARGET_PASSWORD,
    agent,
    clouds,
    fake,
    run_input,
    vpn_plans,
)
from .test_run_input import CREDENTIALS

URL = f"/db-migration/runs/{RUN_ID}"


@pytest.fixture
def client(agent):  # noqa: F811
    app.dependency_overrides[get_client] = agent.client
    yield TestClient(app)
    app.dependency_overrides.pop(get_client, None)


def start(client, body=None):
    response = client.post("/db-migration/runs", json=body or run_input())
    assert response.status_code == 202
    return response


def ended(client):
    wait_until(lambda: RUN_ID not in db_workflow._active)
    return client.get(URL).json()


def ready(client):
    wait_until(lambda: client.get(URL).json()["ready_for_cutover"])
    return client.get(URL).json()


def fail_with(exc):
    def raiser(*_):
        raise exc

    return raiser


# ---- 클라우드 키 등록 ----


def test_a_form_can_be_built_from_the_cloud_list_and_no_value_comes_back(client):
    client.delete("/db-migration/clouds/aws")

    listed = {row["cloud"]: row for row in client.get("/db-migration/clouds").json()}

    assert listed["aws"] == {"cloud": "aws", "registered": False,
                             "required_keys": ["aws_access_key_id", "aws_secret_access_key"]}
    response = client.put("/db-migration/clouds/aws", json=CREDENTIALS["aws"])
    assert response.status_code == 200 and response.json()["registered"] is True
    assert CREDENTIALS["aws"]["aws_secret_access_key"] not in response.text
    assert resolve("secret://cloud/aws") == CREDENTIALS["aws"]


@pytest.mark.parametrize(("cloud", "body"), [
    ("aws", {"aws_access_key_id": "pasted-marker"}),
    ("aws", {"aws_access_key_id": "pasted-marker", "aws_secret_access_key": ""}),
    ("aws", {"aws_access_key_id": "pasted-marker\n", "aws_secret_access_key": "x"}),
    ("aws", {"aws_access_key_id": "a", "aws_secret_access_key": "b", "aws_session_token": "pasted-marker"}),
    ("gcp", {"credentials_json": "pasted-marker"}),
    ("azure", ["pasted-marker"]),
])
def test_wrong_keys_are_refused_without_quoting_them(client, cloud, body):
    before = resolve(f"secret://cloud/{cloud}")

    response = client.put(f"/db-migration/clouds/{cloud}", json=body)

    assert response.status_code == 422 and "pasted-marker" not in response.text
    assert resolve(f"secret://cloud/{cloud}") == before


def test_an_unknown_cloud_is_not_a_route(client):
    assert client.put("/db-migration/clouds/ncp", json={}).status_code == 422


def test_a_run_can_start_right_after_registering(client, vpn_plans):  # noqa: F811
    client.delete("/db-migration/clouds/gcp")
    assert client.post("/db-migration/runs", json=run_input()).status_code == 409
    assert not has_secret("secret://cloud/gcp")

    client.put("/db-migration/clouds/gcp", json={"credentials_json": json.dumps({"project_id": "p"})})

    start(client)
    ready(client)


# ---- 단계와 오류 ----


@pytest.mark.parametrize(("target", "name", "exc", "code", "step"), [
    (cloud_lookup, "find_route", cloud_lookup.LookupFailed("no primary PostgreSQL instance"), "lookup_failed", 4),
    (vpn_workflow, "ensure_vpn", db_workflow.VpnError("tunnel_timeout", "VPN tunnels did not become ready"),
     "tunnel_timeout", 7),
    (vpn_workflow, "ensure_vpn", RuntimeError("detail-marker"), "internal", 7),
])
def test_a_failure_says_its_code_and_where_it_stopped(client, monkeypatch, target, name, exc, code, step):
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: True)
    monkeypatch.setattr(target, name, fail_with(exc))

    start(client)
    status = ended(client)

    assert status["state"] == "failed" and status["step"] == step and status["error"]["code"] == code
    assert status["error"]["message"] == status["message"] == status["metrics"]["cdc_error"]
    assert "detail-marker" not in json.dumps(status)


def test_unreachable_database_stops_at_step_8(client, vpn_plans, agent, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "0")
    agent.unreachable = True

    start(client)
    status = ended(client)

    assert (status["step"], status["error"]["code"]) == (8, "db_unreachable")


def test_a_table_that_cannot_move_stops_at_step_9_with_its_code(client, vpn_plans, agent):  # noqa: F811
    agent.tables = [TABLE, {**TABLE, "name": "audit", "primary_key": []}]

    start(client)
    status = ended(client)

    assert (status["step"], status["error"]["code"]) == (9, "no_primary_key")
    assert "public.audit" in status["error"]["message"]


def test_agent_phases_become_the_last_steps_and_a_failed_job_has_a_code(client, vpn_plans, agent):  # noqa: F811
    start(client)
    status = ready(client)

    assert status["step"] == status["last_step"] == 12 and status["error"] is None

    agent.job.update(state="failed", phase="streaming", message="sync stopped password=hunter2")
    failed = client.get(URL).json()

    assert failed["error"]["code"] == "job_failed" and "hunter2" not in json.dumps(failed)


def test_an_unreachable_agent_has_a_code(client, vpn_plans, agent):  # noqa: F811
    start(client)
    ready(client)
    agent.down = True

    status = client.get(URL).json()

    assert status["state"] == "unknown" and status["error"]["code"] == "status_unknown"


# ---- DB 선택과 재실행 ----


def test_choices_come_as_values_and_retry_needs_no_password_again(client, vpn_plans, agent):  # noqa: F811
    agent.databases["10.1.0.5"] = ["postgres", "shop"]

    start(client)
    status = ended(client)

    assert status["error"]["code"] == "database_selection_required"
    assert status["database_choices"] == {"source": ["postgres", "shop"], "target": ["shop_new"]}

    response = client.post(f"{URL}/retry", json={"source_database": "shop"})

    assert response.status_code == 202 and response.json() == {"run_id": RUN_ID}
    done = ready(client)
    assert agent.request["source"]["database"] == "shop"
    assert agent.secrets["source.admin.password"] == SOURCE_PASSWORD
    assert agent.secrets["target.admin.password"] == TARGET_PASSWORD
    assert done["error"] is None and done["database_choices"] is None


def test_retry_without_a_body_keeps_the_earlier_choice(client, vpn_plans, agent, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "0")
    body = run_input(source_db={"database": "shop"})
    agent.unreachable = True
    start(client, body)
    ended(client)
    agent.unreachable = False

    assert client.post(f"{URL}/retry").status_code == 202

    ready(client)
    assert agent.request["source"]["database"] == "shop"


@pytest.mark.parametrize("name", [" shop", "a\nb"])
def test_retry_refuses_a_bad_database_name(client, name):
    assert client.post(f"{URL}/retry", json={"source_database": name}).status_code == 422


def test_retry_of_a_run_without_saved_inputs_is_refused(client):
    response = client.post(f"{URL}/retry")

    assert response.status_code == 409 and "POST /runs" in response.json()["detail"]
    assert RUN_ID not in db_workflow._active and RUN_ID not in db_workflow._runs


def test_retry_after_the_run_ended_is_refused(client, vpn_plans):  # noqa: F811
    start(client)
    ready(client)
    db_workflow.cutover(RUN_ID, "pm", agent=client.app.dependency_overrides[get_client]())
    wait_until(lambda: RUN_ID not in db_workflow._active)
    client.post(f"{URL}/finish")

    assert client.post(f"{URL}/retry").status_code == 409


# ---- 값 ----


def test_measure_error_reaches_the_screen_without_secrets(client, vpn_plans, agent):  # noqa: F811
    start(client)
    ready(client)
    agent.job["metrics"]["measure_error"] = "could not compare password=hunter2"

    status = client.get(URL).json()

    assert "could not compare" in status["metrics"]["measure_error"]
    assert "hunter2" not in status["metrics"]["measure_error"]


def test_traffic_is_on_the_source_until_a_cutover_is_tried(client, vpn_plans, agent):  # noqa: F811
    start(client)

    assert ready(client)["metrics"]["serving"] == "source"

    agent.job["evidence"] = {"cutover": {"steps": [{"step": "hold"}, {"step": "failed"}]}}
    assert "serving" not in client.get(URL).json()["metrics"]


def test_serving_is_not_guessed_when_a_registered_job_is_lost(client, vpn_plans, agent):  # noqa: F811
    start(client)
    ready(client)
    agent.job = None

    assert "serving" not in client.get(URL).json()["metrics"]


def test_target_network_and_route_survive_a_restart(client, vpn_plans):  # noqa: F811
    start(client)
    ready(client)
    # 재시작하면 메모리의 값이 사라진다. 실행 중인 스레드가 쓰는 항목은 그대로 둔다.
    db_workflow._runs[RUN_ID].target_network = None
    db_workflow._runs[RUN_ID].route = None

    status = client.get(URL).json()

    assert status["metrics"]["target_db_network"] == "PRIVATE"
    assert status["route"]["target"]["cloud"] == "gcp"


def test_nothing_secret_in_any_response(client, vpn_plans):  # noqa: F811
    start(client)
    ready(client)

    shown = client.get(URL).text + client.get("/db-migration/runs").text + client.get("/db-migration/clouds").text

    for secret in (SOURCE_PASSWORD, TARGET_PASSWORD, CREDENTIALS["aws"]["aws_secret_access_key"]):
        assert secret not in shown


# ---- 목록과 API 계약 ----


def test_the_list_holds_runs_until_they_are_clean(client, vpn_plans):  # noqa: F811
    assert client.get("/db-migration/runs").json() == []

    start(client)
    ready(client)

    row = client.get("/db-migration/runs").json()[0]
    assert row["run_id"] == RUN_ID and row["route"]["source"]["cloud"] == "aws"

    db_workflow.cutover(RUN_ID, "pm", agent=client.app.dependency_overrides[get_client]())
    wait_until(lambda: RUN_ID not in db_workflow._active)
    assert client.post(f"{URL}/finish").json()["state"] == "finished"

    assert client.get("/db-migration/runs").json() == []


def test_the_api_document_lists_the_values_a_screen_branches_on():
    components = app.openapi()["components"]["schemas"]
    status = components["MigrationRunStatus"]["properties"]
    metrics = components["RunMetrics"]["properties"]

    assert set(status["phase"]["enum"]) == set(schemas.RUN_PHASES)
    assert "waiting_approval" in status["state"]["enum"]
    assert set(metrics["serving"]["enum"]) == {"source", "target"}
    assert "post_switch_failed" in metrics["cutover_outcome"]["enum"]
    assert {"RunError", "DatabaseChoices", "CloudStatus", "RunSummary"} <= components.keys()


def test_the_listed_phases_and_states_match_the_agent():
    root = Path(__file__).resolve().parents[3]
    models = (root / "infra/db_migration/agent/cdc_agent/models.py").read_text()

    def literal(name: str) -> set[str]:
        block = re.search(rf"^{name} = Literal\[(.*?)\]", models, re.DOTALL | re.MULTILINE).group(1)
        return set(re.findall(r'"([a-z_]+)"', block))

    assert literal("Phase") == set(schemas.AGENT_PHASES)
    assert literal("State") <= set(schemas.RUN_STATES)
