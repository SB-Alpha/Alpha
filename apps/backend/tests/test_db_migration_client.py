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
