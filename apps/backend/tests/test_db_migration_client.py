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


def test_a_missing_agent_url_is_caught_as_key_error_with_a_plain_message(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    with pytest.raises(KeyError) as caught:
        AgentSettings.from_env()
    assert str(caught.value) == "environment variable CDC_AGENT_URL is required"


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
    assert final_job["metrics"]["simulated"] is True
    assert final_job["metrics"]["traffic_mode"] == "TARGET"
    assert final_job["metrics"]["copy_percent"] == 100


def test_simulation_abort_ends_the_job_like_the_real_agent():
    # 추적은 aborted 를 봐야 VPN 을 지우고 끝납니다. 등록 전 작업은 실제 에이전트처럼 404 입니다.
    client = CdcAgentClient(AgentSettings(url="mock://simulation"))
    with pytest.raises(AgentError) as missing:
        client.abort("test-sim-no-job")
    assert missing.value.status_code == 404

    client.create_job({"job_id": "test-sim-abort", "source": {}, "target": {}})

    assert client.abort("test-sim-abort")["state"] == "aborted"
    assert client.get_job("test-sim-abort")["state"] == "aborted"


def test_simulation_flag_does_not_replace_a_real_agent_url(monkeypatch):
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    settings = AgentSettings.from_env()
    assert not settings.is_simulation

    client = CdcAgentClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "real"})))
    assert client.status() == {"status": "real"}


def test_simulation_flag_is_used_only_when_url_is_missing(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    assert AgentSettings.from_env().is_simulation


def test_simulated_state_is_shared_across_clients():
    settings = AgentSettings(url="mock://simulation")
    job = CdcAgentClient(settings).create_job({"job_id": "shared-sim", "source": {}, "target": {}})
    assert CdcAgentClient(settings).get_job(job["job_id"])["job_id"] == "shared-sim"
