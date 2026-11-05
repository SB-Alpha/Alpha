import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.features.provisioning.terraform import ProvisioningError
from app.features.provisioning.worker_client import WorkerClient

TOKEN = "secret-token-value"


class FakeWorkerServer:
    """Worker 의 /control/target 만 흉내 내는 진짜 로컬 HTTP 서버. 받은 요청을 기록한다."""

    def __init__(self, *, reject_with: int | None = None, forget: bool = False) -> None:
        self.url_value: str | None = None
        self.requests: list[tuple[str, str, str | None]] = []  # (method, Authorization, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() or None
                outer.requests.append((method, self.headers.get("Authorization", ""), raw))
                # Cloudflare 는 파이썬 기본 User-Agent 를 막는다 (실제 Worker 에서 403 / error 1010 으로 확인됨)
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    return self._reply(403, {"error": "blocked user agent"})
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply(401, {"error": "unauthorized"})
                if reject_with:
                    return self._reply(reject_with, {"error": "mode is TARGET: the target url cannot change"})
                if method == "POST" and not forget:
                    outer.url_value = json.loads(raw)["url"].rstrip("/")
                self._reply(200, {"url": outer.url_value})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_set_target_posts_the_url_then_reads_it_back():
    with FakeWorkerServer() as worker:
        WorkerClient(worker.base_url, TOKEN).set_target("http://127.0.0.1:9003/")
    assert [r[0] for r in worker.requests] == ["POST", "GET"]
    assert json.loads(worker.requests[0][2]) == {"url": "http://127.0.0.1:9003/"}
    assert all(r[1] == f"Bearer {TOKEN}" for r in worker.requests)
    assert worker.url_value == "http://127.0.0.1:9003"


def test_set_target_fails_if_the_worker_did_not_keep_the_value():
    with FakeWorkerServer(forget=True) as worker, pytest.raises(ProvisioningError, match="다릅니다"):
        WorkerClient(worker.base_url, TOKEN).set_target("http://127.0.0.1:9003")


def test_a_rejection_becomes_an_error_with_the_status_and_reason():
    with FakeWorkerServer(reject_with=409) as worker, pytest.raises(ProvisioningError, match="409.*TARGET"):
        WorkerClient(worker.base_url, TOKEN).set_target("http://127.0.0.1:9003")


def test_a_wrong_token_is_an_error_and_the_token_is_never_in_the_message():
    with FakeWorkerServer() as worker, pytest.raises(ProvisioningError) as caught:
        WorkerClient(worker.base_url, "wrong-token-value").set_target("http://127.0.0.1:9003")
    assert "401" in str(caught.value)
    assert "wrong-token-value" not in str(caught.value)


def test_an_unreachable_worker_is_an_error_without_the_token():
    with pytest.raises(ProvisioningError, match="연결") as caught:
        WorkerClient("http://127.0.0.1:1", TOKEN).get_target()
    assert TOKEN not in str(caught.value)


def test_from_env_is_none_without_a_url(monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    assert WorkerClient.from_env() is None


def test_from_env_reads_the_token_from_a_file(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    monkeypatch.setenv("WORKER_URL", "http://127.0.0.1:8787/")
    monkeypatch.setenv("WORKER_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("WORKER_TOKEN", raising=False)
    client = WorkerClient.from_env()
    assert client is not None and client.base_url == "http://127.0.0.1:8787"
    assert client._token == TOKEN


def test_from_env_without_a_token_is_an_error(monkeypatch):
    monkeypatch.setenv("WORKER_URL", "http://127.0.0.1:8787")
    monkeypatch.delenv("WORKER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("WORKER_TOKEN", raising=False)
    with pytest.raises(ProvisioningError, match="토큰"):
        WorkerClient.from_env()
