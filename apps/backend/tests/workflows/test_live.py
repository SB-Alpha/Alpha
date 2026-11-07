"""GET /live/edge: Worker·CDC 에이전트·프로버 상태를 지어내지 않고, 비밀을 싣지 않는지 봅니다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.features.db_migration import client as agent_client
from app.features.db_migration.client import AgentError
from app.features.provisioning import worker_client
from app.features.provisioning.terraform import ProvisioningError
from app.features.workflows import live, prober
from app.features.workflows.runners import SIMULATION_UNSUPPORTED_MESSAGE
from app.main import app

TOKEN = "tok-super-secret-123"
LIVE_ENVS = (
    "WORKER_URL",
    "WORKER_TOKEN",
    "WORKER_TOKEN_FILE",
    "CDC_AGENT_URL",
    "CDC_AGENT_ACCESS_CLIENT_ID",
    "CDC_AGENT_ACCESS_CLIENT_SECRET",
    "ALPHA_CDC_SIMULATION",
    "ALPHA_PROBE_URL",
    "CDC_USE_EXISTING_VPN",
)

WORKER_STATUS = {
    "mode": "HOLD",
    "next_origin": "TARGET",
    "run_id": "mig-1",
    "hold_started_at": "2026-11-08T01:00:00.000Z",
    "released_at": None,
    "held_count": 3,
    "updated_at": "2026-11-08T01:00:00.000Z",
    "target_url": "https://secret-target.run.app",
    "arbitrage": {"enabled": False},
    "quarantine": {
        "isolated_nodes": ["source_db"],
        "reroute_target": "https://reroute.example.com",
        "quarantined_at": None,
    },
    "inflight_source": 1,
    "request_counts": {
        "since": "2026-11-08T00:59:00.000Z",
        "write_total": 10,
        "write_failed": 1,
        "read_total": 20,
        "read_failed": 0,
        "token": TOKEN,
    },
    "leaked": TOKEN,
}

AGENT_STATUS = {
    "node": "cdc-1",
    "kafka": True,
    "connect": True,
    "plugins_missing": [],
    "worker_configured": True,
    "jobs": {"mig_1": {"state": "running", "phase": "streaming"}},
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in LIVE_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALPHA_LIVE_PROBE", "1")  # 아래 프로브 테스트는 켠 상태를 봅니다. 꺼진 기본값은 따로 확인합니다
    live.reset_cache()
    yield
    live.reset_cache()


@pytest.fixture
def client():
    return TestClient(app)


def _fake_worker_status(monkeypatch, result):
    calls = []

    def status(self, *, timeout_s):
        calls.append(timeout_s)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(worker_client.WorkerClient, "status", status)
    return calls


def _fake_agent_status(monkeypatch, result):
    seen = []

    def status(self):
        seen.append(self._http.timeout.read)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent_client.CdcAgentClient, "status", status)
    return seen


# --- 설정 없음 --------------------------------------------------------------


def test_nothing_configured_is_not_configured_with_null_values(client):
    body = client.get("/live/edge").json()

    assert body["checked_at"].endswith("Z")
    assert body["worker"] == {
        "status": "not_configured",
        "error": None,
        "mode": None,
        "next_origin": None,
        "hold_started_at": None,
        "released_at": None,
        "held_count": None,
        "inflight_source": None,
        "request_counts": None,
        "isolated_nodes": None,
    }
    assert body["agent"] == {
        "status": "not_configured",
        "error": None,
        "kafka": None,
        "connect": None,
        "plugins_missing": None,
    }
    assert body["probe"] == {"status": "not_configured", "error": None, "ok": None, "latency_ms": None, "origin": None}


# --- Worker -----------------------------------------------------------------


def test_worker_ok_picks_only_safe_fields(client, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    calls = _fake_worker_status(monkeypatch, WORKER_STATUS)

    res = client.get("/live/edge")
    worker = res.json()["worker"]

    assert worker == {
        "status": "ok",
        "error": None,
        "mode": "HOLD",
        "next_origin": "TARGET",
        "hold_started_at": "2026-11-08T01:00:00.000Z",
        "released_at": None,
        "held_count": 3,
        "inflight_source": 1,
        "request_counts": {
            "since": "2026-11-08T00:59:00.000Z",
            "write_total": 10,
            "write_failed": 1,
            "read_total": 20,
            "read_failed": 0,
        },
        "isolated_nodes": ["source_db"],
    }
    assert calls == [live.CALL_TIMEOUT_S]
    for secret in (TOKEN, "secret-target", "reroute.example.com", "gate.example.workers.dev"):
        assert secret not in res.text


def test_worker_url_without_token_is_error(client, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    worker = client.get("/live/edge").json()["worker"]
    assert worker["status"] == "error"
    assert worker["error"].startswith("ProvisioningError:")
    assert worker["mode"] is None


def test_worker_http_error_hides_provider_detail(client, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    _fake_worker_status(monkeypatch, ProvisioningError("Worker status 명령 실패 (HTTP 401) raw provider text"))

    res = client.get("/live/edge")
    worker = res.json()["worker"]

    assert worker["status"] == "error"
    assert worker["error"] == "ProvisioningError: Worker status 명령 실패 (HTTP 401)"
    assert "raw provider text" not in res.text
    assert worker["request_counts"] is None


def test_worker_unexpected_exception_reports_type_only(client, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    _fake_worker_status(monkeypatch, RuntimeError(f"boom https://gate.example.workers.dev?token={TOKEN}"))

    res = client.get("/live/edge")

    assert res.json()["worker"]["error"] == "RuntimeError"
    assert TOKEN not in res.text


# --- 에이전트 ----------------------------------------------------------------


def test_agent_ok_reports_status_fields_with_short_timeout(client, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://agent.example.com")
    seen = _fake_agent_status(monkeypatch, {**AGENT_STATUS, "plugins_missing": ["jdbc-sink"]})

    agent = client.get("/live/edge").json()["agent"]

    assert agent == {
        "status": "ok",
        "error": None,
        "kafka": True,
        "connect": True,
        "plugins_missing": ["jdbc-sink"],
    }
    assert seen and seen[0] <= live.CALL_TIMEOUT_S


def test_agent_connect_down_keeps_null_plugins(client, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://agent.example.com")
    _fake_agent_status(monkeypatch, {**AGENT_STATUS, "connect": False, "kafka": False, "plugins_missing": None})
    agent = client.get("/live/edge").json()["agent"]
    assert agent["connect"] is False and agent["kafka"] is False and agent["plugins_missing"] is None


def test_agent_error_hides_detail(client, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://agent.example.com")
    _fake_agent_status(monkeypatch, AgentError(502, "<html>provider body</html>"))

    res = client.get("/live/edge")
    agent = res.json()["agent"]

    assert agent["status"] == "error"
    assert agent["error"] == "AgentError: HTTP 502"
    assert "provider body" not in res.text
    assert agent["kafka"] is None


def test_mock_agent_url_is_config_error(client, monkeypatch):
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    called = _fake_agent_status(monkeypatch, AGENT_STATUS)

    agent = client.get("/live/edge").json()["agent"]

    assert agent["status"] == "error"
    assert agent["error"] == SIMULATION_UNSUPPORTED_MESSAGE
    assert agent["kafka"] is None
    assert called == []  # 모의 에이전트에는 묻지 않습니다


# --- 프로버 ------------------------------------------------------------------


def test_probe_ok(client, monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "https://gate.example.com/health")
    seen = []

    def probe_once(self, timeout_s=None):
        seen.append(timeout_s)
        return {"ok": False, "latency_ms": 12.3, "origin": "SOURCE"}

    monkeypatch.setattr(prober.TrafficProber, "probe_once", probe_once)

    probe = client.get("/live/edge").json()["probe"]

    assert probe == {"status": "ok", "error": None, "ok": False, "latency_ms": 12.3, "origin": "SOURCE"}
    assert seen == [live.CALL_TIMEOUT_S]


def test_probe_unusable_url_is_error(client, monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "https://user:pw@example.com/")
    res = client.get("/live/edge")
    probe = res.json()["probe"]
    assert probe["status"] == "error"
    assert probe["latency_ms"] is None
    assert "pw@" not in res.text


def test_probe_exception_is_error(client, monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "https://gate.example.com/health")

    def probe_once(self, timeout_s=None):
        raise ValueError("https://gate.example.com/health broke")

    monkeypatch.setattr(prober.TrafficProber, "probe_once", probe_once)
    res = client.get("/live/edge")
    assert res.json()["probe"] == {
        "status": "error",
        "error": "ValueError",
        "ok": None,
        "latency_ms": None,
        "origin": None,
    }
    assert "gate.example.com" not in res.text


# --- 캐시 --------------------------------------------------------------------


def test_snapshot_is_cached_for_one_second(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(live, "_clock", lambda: now[0])
    builds = []
    real_build = live._build_snapshot

    def counting_build():
        builds.append(1)
        return real_build()

    monkeypatch.setattr(live, "_build_snapshot", counting_build)

    first = live.edge_snapshot()
    now[0] = 100.9
    assert live.edge_snapshot() is first
    now[0] = 101.1
    refreshed = live.edge_snapshot()

    assert refreshed is not first
    assert len(builds) == 2


def test_slow_sub_call_is_cut_off(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(live, "CALL_TIMEOUT_S", 0.1)

    def slow_worker():
        release.wait(5)
        return live.WorkerLive(status="ok")

    monkeypatch.setattr(live, "_worker_live", slow_worker)
    try:
        snapshot = live.edge_snapshot()
    finally:
        release.set()

    assert snapshot.worker.status == "error"
    assert snapshot.worker.error.startswith("TimeoutError")


def test_route_requires_local_access():
    from app.core.local_access import require_local_access

    saved = app.dependency_overrides.pop(require_local_access, None)
    try:
        res = TestClient(app, base_url="http://evil.example.com").get("/live/edge")
    finally:
        if saved is not None:
            app.dependency_overrides[require_local_access] = saved
    assert res.status_code == 403


def test_agent_http_client_is_closed_after_each_call(client, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://agent.example.com")
    clients = []

    def status(self):
        clients.append(self._http)
        return AGENT_STATUS

    monkeypatch.setattr(agent_client.CdcAgentClient, "status", status)
    assert client.get("/live/edge").json()["agent"]["status"] == "ok"
    assert clients and clients[0].is_closed


def _count_probes(monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "https://gate.example.com/health")
    sent = []

    def probe_once(self, timeout_s=None):
        sent.append(timeout_s)
        return {"ok": True, "latency_ms": 5.0, "origin": "SOURCE"}

    monkeypatch.setattr(prober.TrafficProber, "probe_once", probe_once)
    return sent


@pytest.mark.parametrize("worker_result", [{**WORKER_STATUS, "mode": "HOLD"}, RuntimeError("down")])
def test_probe_is_not_sent_while_the_gate_holds_or_is_unknown(client, monkeypatch, worker_result):
    # HOLD 중 프로브는 held_count·read_total 에 섞여 전환 증적을 흐립니다. 상태를 모를 때도 보내지 않습니다.
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    _fake_worker_status(monkeypatch, worker_result)
    sent = _count_probes(monkeypatch)

    probe = client.get("/live/edge").json()["probe"]

    assert sent == []
    assert probe["status"] == "skipped"
    assert probe["latency_ms"] is None


@pytest.mark.parametrize("mode", ["SOURCE", "TARGET"])
def test_probe_is_sent_when_the_gate_is_not_holding(client, monkeypatch, mode):
    monkeypatch.setenv("WORKER_URL", "https://gate.example.workers.dev")
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    _fake_worker_status(monkeypatch, {**WORKER_STATUS, "mode": mode})
    sent = _count_probes(monkeypatch)

    probe = client.get("/live/edge").json()["probe"]

    assert len(sent) == 1
    assert probe["status"] == "ok"


def test_probe_is_sent_without_a_worker(client, monkeypatch):
    sent = _count_probes(monkeypatch)
    assert client.get("/live/edge").json()["probe"]["status"] == "ok"
    assert len(sent) == 1


def test_live_probe_is_off_by_default_so_the_shared_worker_gets_no_dashboard_traffic(client, monkeypatch):
    monkeypatch.delenv("ALPHA_LIVE_PROBE")
    sent = _count_probes(monkeypatch)
    probe = client.get("/live/edge").json()["probe"]
    assert sent == []
    assert probe["status"] == "not_configured" and "ALPHA_LIVE_PROBE" in probe["error"]
