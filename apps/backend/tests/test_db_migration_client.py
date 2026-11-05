import httpx
import pytest

from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient


def make_client(handler) -> CdcAgentClient:
    settings = AgentSettings(url="https://cdc.example", access_client_id="id", access_client_secret="secret")
    return CdcAgentClient(settings, transport=httpx.MockTransport(handler))


def test_sends_access_headers_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(202, json={"job_id": "job01", "state": "running", "phase": "cutover"})

    job = make_client(handler).cutover("job01", approved_by="pm")
    assert job["phase"] == "cutover"
    assert seen["headers"]["CF-Access-Client-Id"] == "id"
    assert seen["headers"]["CF-Access-Client-Secret"] == "secret"
    assert b'"approved_by":"pm"' in seen["body"].replace(b" ", b"")


def test_error_carries_agent_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "job is running/streaming"})

    with pytest.raises(AgentError) as exc:
        make_client(handler).cutover("job01", approved_by="pm")
    assert exc.value.status_code == 409
    assert "running/streaming" in exc.value.detail


def test_wait_for_returns_on_phase_and_raises_on_failure():
    phases = iter(["snapshot", "streaming", "cutover_ready"])

    def ready(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "running", "phase": next(phases), "message": ""})

    job = make_client(ready).wait_for("job01", {"cutover_ready"}, timeout_s=5, interval_s=0)
    assert job["phase"] == "cutover_ready"

    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "failed", "phase": "precheck", "message": "wal_level"})

    with pytest.raises(AgentError, match="wal_level"):
        make_client(failed).wait_for("job01", {"cutover_ready"}, timeout_s=5, interval_s=0)


def test_simulation_mode_full_lifecycle():
    client = CdcAgentClient(AgentSettings(url="mock://simulation"))

    # 1. status check
    st = client.status()
    assert st["status"] == "ok"
    assert st["mode"] == "simulation"

    # 2. probe check
    probes = client.probe([{"host": "10.0.0.1", "port": 5432}])
    assert len(probes) == 1
    assert probes[0]["ok"] is True

    # 3. create job
    job = client.create_job({"job_id": "test-sim-01", "source": {}, "target": {}})
    assert job["job_id"] == "test-sim-01"
    assert job["state"] == "running"
    assert job["phase"] == "snapshot"

    # 4. poll job -> transitions to streaming
    job = client.get_job("test-sim-01")
    assert job["state"] == "running"

    # 5. cutover
    resp = client.cutover("test-sim-01", approved_by="operator-pj")
    assert resp["phase"] in ("switching", "stabilizing")

    # 6. poll job after cutover -> stabilizing / succeeded
    final_job = client.get_job("test-sim-01")
    assert final_job["phase"] == "stabilizing"
    assert final_job["state"] == "succeeded"
    assert final_job["metrics"]["serving"] == "gcp"
    assert final_job["metrics"]["copy_percent"] == 100
