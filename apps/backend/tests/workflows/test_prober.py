"""프로버는 실제 HTTP 서버를 상대로 시험합니다. 지표가 진짜 응답에서 나오는지 보기 위해서입니다."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.features.workflows.prober import USER_AGENT, ProbedContext, TrafficProber, prober_from_env


class Upstream:
    """응답 코드·지연·출처를 시험 중에 바꿀 수 있는 서버."""

    def __init__(self) -> None:
        self.status = 200
        self.delay_s = 0.0
        self.origin: str | None = "SOURCE"
        self.user_agent: str | None = None
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                upstream.user_agent = self.headers.get("User-Agent")
                time.sleep(upstream.delay_s)
                self.send_response(upstream.status)
                if upstream.origin:
                    self.send_header("X-Gate-Origin", upstream.origin)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def upstream():
    server = Upstream()
    yield server
    server.server.shutdown()
    server.server.server_close()


def _wait_for(prober: TrafficProber, count: int) -> None:
    deadline = time.monotonic() + 5
    while prober.snapshot().get("probe_requests", 0) < count:
        assert time.monotonic() < deadline, "프로버가 요청을 보내지 않았습니다"
        time.sleep(0.005)


def _measure(url: str, count: int = 3, timeout_s: float = 2.0) -> dict:
    prober = TrafficProber(url, interval_s=0.005, timeout_s=timeout_s)
    prober.start()
    _wait_for(prober, count)
    return prober.stop()


def test_healthy_upstream_reports_zero_failures(upstream):
    metrics = _measure(upstream.url)
    assert metrics["failed_requests"] == 0
    assert metrics["probe_requests"] >= 3
    assert metrics["probe_last_origin"] == "SOURCE"


def test_sends_its_own_user_agent_so_cloudflare_does_not_block_it(upstream):
    _measure(upstream.url, count=1)
    assert upstream.user_agent == USER_AGENT
    assert not upstream.user_agent.startswith("Python-urllib")


def test_server_errors_are_counted_as_failures(upstream):
    upstream.status = 502
    metrics = _measure(upstream.url)
    assert metrics["failed_requests"] == metrics["probe_requests"] >= 3


def test_client_errors_are_failures_so_a_wrong_path_cannot_look_healthy(upstream):
    upstream.status = 404
    metrics = _measure(upstream.url)
    assert metrics["failed_requests"] == metrics["probe_requests"]


def test_unreachable_upstream_is_counted_not_raised():
    metrics = _measure("http://127.0.0.1:9/", timeout_s=0.2)
    assert metrics["failed_requests"] == metrics["probe_requests"] >= 3


def test_slow_response_shows_in_max_latency(upstream):
    upstream.delay_s = 0.05
    metrics = _measure(upstream.url, count=1)
    assert metrics["probe_max_ms"] >= 50
    assert metrics["probe_p99_ms"] <= metrics["probe_max_ms"]


def test_timeout_is_a_failure(upstream):
    upstream.delay_s = 0.3
    metrics = _measure(upstream.url, count=1, timeout_s=0.05)
    assert metrics["failed_requests"] >= 1


def test_origin_follows_the_switch(upstream):
    prober = TrafficProber(upstream.url, interval_s=0.005, timeout_s=2.0)
    prober.start()
    _wait_for(prober, 1)
    upstream.origin = "TARGET"
    before = prober.snapshot()["probe_requests"]
    _wait_for(prober, before + 2)
    assert prober.stop()["probe_last_origin"] == "TARGET"


def test_missing_origin_header_reports_no_origin(upstream):
    upstream.origin = None
    assert "probe_last_origin" not in _measure(upstream.url)


def test_no_samples_reports_nothing():
    assert TrafficProber("http://127.0.0.1:9/").snapshot() == {}


def test_requests_stop_after_stop(upstream):
    prober = TrafficProber(upstream.url, interval_s=0.005, timeout_s=2.0)
    prober.start()
    _wait_for(prober, 1)
    final = prober.stop()
    time.sleep(0.05)
    assert prober.snapshot() == final


class RecordingCtx:
    def __init__(self) -> None:
        self.reports: list[tuple] = []

    def report(self, state, progress, metrics=None, message=None):
        self.reports.append((state, progress, metrics, message))


def test_context_adds_measurements_and_finish_keeps_last_progress(upstream):
    prober = TrafficProber(upstream.url, interval_s=0.005, timeout_s=2.0)
    prober.start()
    _wait_for(prober, 1)
    ctx = RecordingCtx()
    probed = ProbedContext(ctx, prober)

    probed.report("running", 40, {"traffic_mode": "SOURCE"}, "전환 중")
    assert ctx.reports[-1][2]["traffic_mode"] == "SOURCE"
    assert ctx.reports[-1][2]["failed_requests"] == 0

    probed.finish()
    assert ctx.reports[-1][1] == 40
    assert ctx.reports[-1][3] is None


def test_measured_value_overrides_module_report(upstream):
    upstream.status = 500
    prober = TrafficProber(upstream.url, interval_s=0.005, timeout_s=2.0)
    prober.start()
    _wait_for(prober, 1)
    ctx = RecordingCtx()
    ProbedContext(ctx, prober).report("running", 10, {"failed_requests": 0})
    prober.stop()
    assert ctx.reports[-1][2]["failed_requests"] >= 1


@pytest.mark.parametrize("value", ["", "   ", "ftp://example.com/", "not a url", "https://user:pw@example.com/"])
def test_env_without_usable_url_gives_no_prober(monkeypatch, value):
    monkeypatch.setenv("ALPHA_PROBE_URL", value)
    assert prober_from_env() is None


def test_env_unset_gives_no_prober(monkeypatch):
    monkeypatch.delenv("ALPHA_PROBE_URL", raising=False)
    assert prober_from_env() is None


def test_env_url_gives_prober(monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "https://gate.example.com/health")
    assert prober_from_env() is not None
