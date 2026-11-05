import httpx
from fastapi.testclient import TestClient

from app.features.db_migration import router as db_migration
from app.features.db_migration.client import AgentSettings, CdcAgentClient
from app.main import app

JOB = {
    "job_id": "job01",
    "source": {"host": "src", "database": "postgres", "tables": ["public.work_items"], "app_role": "alpha"},
    "target": {"host": "tgt", "database": "app"},
}


def use_agent(handler) -> TestClient:
    agent = CdcAgentClient(AgentSettings(url="https://cdc.example"), transport=httpx.MockTransport(handler))
    app.dependency_overrides[db_migration.get_client] = lambda: agent
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_create_job_forwards_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(202, json={"job_id": "job01", "state": "pending", "phase": "precheck"})

    resp = use_agent(handler).post("/db-migration/jobs", json=JOB)
    assert resp.status_code == 202
    assert resp.json()["phase"] == "precheck"
    assert seen["path"] == "/jobs"
    assert b'"max_hold_ms":3000' in seen["body"].replace(b" ", b"")


def test_agent_conflict_keeps_status_and_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "job is running/streaming"})

    resp = use_agent(handler).post("/db-migration/jobs/job01/cutover", json={"approved_by": "pm"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "job is running/streaming"}


def test_unreachable_agent_is_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    resp = use_agent(handler).get("/db-migration/status")
    assert resp.status_code == 502


def test_missing_config_is_503(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    db_migration._client.cache_clear()
    resp = TestClient(app).get("/db-migration/status")
    assert resp.status_code == 503
