from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.features.workflows.engine import Engine
from app.features.workflows.router import get_engine
from app.features.workflows.store import RunStore
from app.main import app

BODY = {"github_repo_url": "https://github.com/example/target-app", "aws_region": "ap-northeast-2"}


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = Engine(RunStore(tmp_path / "workflows.sqlite3"))
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_create_run_then_pauses_at_unimplemented_discovery(client):
    created = client.post("/runs", json=BODY)
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    # TestClient 는 응답 뒤 백그라운드 작업(advance)까지 끝낸 상태
    run = client.get(f"/runs/{run_id}").json()
    assert run["state"] == "paused"
    assert run["current_stage"] == "discovery"
    assert run["stages"][0]["state"] == "pending"
    assert [r["run_id"] for r in client.get("/runs").json()] == [run_id]


def test_manual_inputs_reach_plan_approval_and_approve(client):
    run_id = client.post("/runs", json=BODY).json()["run_id"]

    for stage in ("discovery", "planning"):
        res = client.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": {"stage": stage}})
        assert res.status_code == 200

    run = client.get(f"/runs/{run_id}").json()
    assert run["state"] == "waiting_approval"
    assert run["waiting_gate"] == "plan"
    assert run["stages"][1]["manual"] is True

    assert client.post(f"/runs/{run_id}/approve", json={"gate": "cutover"}).status_code == 409
    assert client.post(f"/runs/{run_id}/approve", json={"gate": "plan"}).status_code == 200
    run = client.get(f"/runs/{run_id}").json()
    assert run["state"] == "paused"
    assert run["current_stage"] == "provisioning"


def test_abort_then_commands_conflict(client):
    run_id = client.post("/runs", json=BODY).json()["run_id"]

    aborted = client.post(f"/runs/{run_id}/abort", json={"reason": "테스트"})
    assert aborted.status_code == 200
    assert aborted.json()["state"] == "aborted"
    assert client.post(f"/runs/{run_id}/abort").status_code == 409
    assert client.post(f"/runs/{run_id}/stages/discovery/manual", json={"output": {}}).status_code == 409


def test_errors_for_unknown_run_and_bad_input(client):
    assert client.get("/runs/mig-missing").status_code == 404
    assert client.post("/runs/mig-missing/abort").status_code == 404
    assert client.post("/runs", json={"aws_region": "ap-northeast-2"}).status_code == 422

    run_id = client.post("/runs", json=BODY).json()["run_id"]
    assert client.post(f"/runs/{run_id}/stages/unknown/manual", json={"output": {}}).status_code == 422
    assert client.post(f"/runs/{run_id}/approve", json={"gate": "other"}).status_code == 422


def test_openapi_lists_workflows_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/runs", "/runs/{run_id}", "/runs/{run_id}/approve", "/runs/{run_id}/abort"} <= set(paths)
