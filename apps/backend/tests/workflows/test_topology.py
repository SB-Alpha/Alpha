"""run 구성도는 요청·단계 출력·단계 상태·환경 설정 유무에서만 만들고, 모르는 값은 null 로 둡니다."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.features.workflows.engine import Engine
from app.features.workflows.router import get_engine
from app.features.workflows.schemas import (
    MigrationRequest,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from app.features.workflows.store import RunStore
from app.features.workflows.topology import build_topology
from app.main import app

NODE_IDS = ["source_app", "source_db", "cdc_agent", "vpn", "target_db", "target_app"]
NODE_KEYS = {"id", "kind", "label", "cloud", "region", "resource", "state"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("WORKER_URL", "WORKER_TOKEN", "WORKER_TOKEN_FILE", "CDC_USE_EXISTING_VPN"):
        monkeypatch.delenv(name, raising=False)


def make_run(request: MigrationRequest, outputs: dict | None = None, states: dict | None = None) -> RunStatus:
    now = datetime.now(timezone.utc)
    states = states or {}
    return RunStatus(
        run_id="mig-topology",
        state=WorkflowState.RUNNING,
        current_stage=StageName.DB_MIGRATION,
        request=request,
        stages=[StageStatus(stage=s, state=states.get(s, WorkflowState.PENDING)) for s in StageName],
        outputs=outputs or {},
        created_at=now,
        updated_at=now,
    )


def by_id(topology: dict) -> dict[str, dict]:
    return {node["id"]: node for node in topology["nodes"]}


AWS_REQUEST = MigrationRequest(
    aws_region="ap-northeast-2",
    ecs_service="orders-api",
    rds_instance_id="orders-db",
    aws_credentials_ref="secret://aws",
)

AWS_OUTPUTS = {
    "planning": {
        "source": {
            "account_id": "111122223333",
            "region": "ap-northeast-2",
            "db": {"instance_id": "orders-db-prod", "host": "orders.abc.rds.amazonaws.com", "port": 5432},
        },
        "plan": {"target_infra": {"region": "asia-northeast3", "service_name": "orders-api-gcp"}},
    },
    "provisioning": {
        "target": {
            "cloud": "gcp",
            "project_id": "proj-1",
            "region": "asia-northeast3",
            "db": {"instance": "orders-sql", "private_ip": "10.10.0.5", "database": "orders"},
        }
    },
    "app_deploy": {"app_url": "https://orders-api-gcp-xyz.a.run.app", "healthy": True, "target": {}},
}


def test_aws_to_gcp_run_uses_outputs_and_stage_states():
    run = make_run(
        AWS_REQUEST,
        AWS_OUTPUTS,
        {
            StageName.PLANNING: WorkflowState.SUCCEEDED,
            StageName.PROVISIONING: WorkflowState.SUCCEEDED,
            StageName.DB_MIGRATION: WorkflowState.RUNNING,
        },
    )
    topology = build_topology(run)
    nodes = by_id(topology)

    assert [n["id"] for n in topology["nodes"]] == NODE_IDS
    assert all(set(n) == NODE_KEYS for n in topology["nodes"])
    assert nodes["source_app"] == {
        "id": "source_app",
        "kind": "app",
        "label": "원본 앱",
        "cloud": "aws",
        "region": "ap-northeast-2",
        "resource": "orders-api",
        "state": "succeeded",
    }
    assert nodes["source_db"]["resource"] == "orders-db-prod"  # 조사 결과가 요청값보다 우선
    assert nodes["target_db"] == {
        "id": "target_db",
        "kind": "database",
        "label": "목적지 DB",
        "cloud": "gcp",
        "region": "asia-northeast3",
        "resource": "orders-sql",
        "state": "succeeded",
    }
    assert nodes["target_app"]["resource"] == "orders-api-gcp"
    assert nodes["target_app"]["state"] == "pending"
    assert nodes["cdc_agent"]["state"] == "running"
    assert nodes["cdc_agent"]["cloud"] is None and nodes["cdc_agent"]["resource"] is None
    assert nodes["vpn"]["label"] == "VPN"
    # 주소·사설 IP 는 싣지 않습니다
    text = repr(topology)
    assert "10.10.0.5" not in text and "rds.amazonaws.com" not in text and "run.app" not in text

    edges = {(e["from"], e["to"]): e for e in topology["edges"]}
    assert set(edges) == {
        ("source_app", "source_db"),
        ("source_db", "cdc_agent"),
        ("cdc_agent", "target_db"),
        ("target_app", "target_db"),
    }
    assert edges[("source_db", "cdc_agent")]["via"] == "vpn"


def test_worker_and_existing_vpn_come_from_env(monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("CDC_USE_EXISTING_VPN", "1")
    topology = build_topology(make_run(AWS_REQUEST, AWS_OUTPUTS, {StageName.CUTOVER: WorkflowState.RUNNING}))
    nodes = by_id(topology)

    assert nodes["vpn"]["label"] == "기존 VPN"
    assert nodes["edge_worker"]["cloud"] == "cloudflare"
    assert nodes["edge_worker"]["state"] == "running"
    assert nodes["edge_worker"]["resource"] is None
    assert "gate.example.workers.dev" not in repr(topology)
    pairs = {(e["from"], e["to"]) for e in topology["edges"]}
    assert {("edge_worker", "source_app"), ("edge_worker", "target_app")} <= pairs


def test_gcp_source_run_reads_cloud_source():
    request = MigrationRequest(
        source_kind="gcp",
        cloud_source={"region": "us-central1", "app_name": "shop", "db_instance": "shop-sql", "project_id": "src"},
        target_infra={"provider": "azure", "region": "koreacentral"},
        target_region="koreacentral",
    )
    nodes = by_id(build_topology(make_run(request)))

    assert nodes["source_app"]["cloud"] == "gcp"
    assert nodes["source_app"]["region"] == "us-central1"
    assert nodes["source_app"]["resource"] == "shop"
    assert nodes["source_db"]["resource"] == "shop-sql"
    assert nodes["target_app"]["cloud"] == "azure"
    assert nodes["target_app"]["region"] == "koreacentral"


def test_missing_outputs_leave_values_null():
    request = MigrationRequest(aws_region="ap-northeast-2")
    topology = build_topology(make_run(request))
    nodes = by_id(topology)

    assert "edge_worker" not in nodes
    assert all(e["from"] != "edge_worker" for e in topology["edges"])
    assert nodes["source_app"]["resource"] is None
    assert nodes["source_db"]["resource"] is None
    assert nodes["target_db"]["resource"] is None
    assert nodes["target_app"]["resource"] is None
    # 목적지 클라우드는 요청 규칙(target_infra 가 없으면 gcp)과 target_region 에서 옵니다
    assert nodes["target_db"]["cloud"] == "gcp"
    assert nodes["target_db"]["region"] == "asia-northeast3"
    assert all(n["state"] == "pending" for n in topology["nodes"])


def test_source_app_falls_back_to_plan_source_service_name():
    request = MigrationRequest(aws_region="ap-northeast-2")
    outputs = {"planning": {"plan": {"target_infra": {"service_name": "orders-gcp", "source_service_name": "orders"}}}}
    nodes = by_id(build_topology(make_run(request, outputs)))
    assert nodes["source_app"]["resource"] == "orders"
    assert nodes["target_app"]["resource"] == "orders-gcp"


def test_manual_fixture_outputs_do_not_break():
    run = make_run(
        AWS_REQUEST,
        {"planning": {"stage": "planning", "source": "not-a-dict"}, "provisioning": ["odd"], "app_deploy": None},
    )
    nodes = by_id(build_topology(run))
    assert nodes["source_db"]["resource"] == "orders-db"  # 요청값으로 돌아감
    assert nodes["target_db"]["resource"] is None


# --- API ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    engine = Engine(RunStore(tmp_path / "workflows.sqlite3"))
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_engine, None)


def test_topology_route_returns_shape(client):
    run_id = client.post("/runs", json={"aws_region": "ap-northeast-2", "ecs_service": "orders-api"}).json()["run_id"]

    res = client.get(f"/runs/{run_id}/topology")

    assert res.status_code == 200
    body = res.json()
    assert [n["id"] for n in body["nodes"]] == NODE_IDS
    assert by_id(body)["source_app"]["resource"] == "orders-api"
    edge = next(e for e in body["edges"] if e["id"] == "source_db-cdc_agent")
    assert edge == {"id": "source_db-cdc_agent", "from": "source_db", "to": "cdc_agent", "kind": "replication", "via": "vpn"}


def test_topology_route_404_for_unknown_run(client):
    assert client.get("/runs/nope/topology").status_code == 404


def test_topology_route_requires_local_access(client):
    from app.core.local_access import require_local_access

    saved = app.dependency_overrides.pop(require_local_access, None)
    try:
        res = TestClient(app, base_url="http://evil.example.com").get("/runs/any/topology")
    finally:
        if saved is not None:
            app.dependency_overrides[require_local_access] = saved
    assert res.status_code == 403
