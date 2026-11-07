"""가짜 CDC 에이전트. httpx.MockTransport 로 실제 CdcAgentClient 를 그대로 씁니다.

에이전트 계약(db_migration client 와 infra/db_migration/agent)의 응답 모양을 따릅니다.
작업은 snapshot → streaming(cutover_ready) → 전환 요청 → stabilizing/succeeded 순으로 진행합니다.
실제 값처럼 보이는 지표만 돌려주고, 시뮬레이션 표식(simulated)은 싣지 않습니다.

테스트마다 새 인스턴스를 씁니다. db_migration 과 cutover 가 클라이언트를 따로 만들어도 같은 작업을 보게
install() 이 그 테스트의 전송 계층 하나를 모든 클라이언트에 넘깁니다.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

FAKE_AGENT_URL = "http://fake-agent"

_TABLES_SNAPSHOT = {
    "users": {"source_rows": 500, "target_rows": 150, "percent": 30, "match": False},
    "orders": {"source_rows": 2000, "target_rows": 600, "percent": 30, "match": False},
}
_TABLES_STREAMING = {
    "users": {"source_rows": 500, "target_rows": 500, "percent": 100, "match": True},
    "orders": {"source_rows": 2000, "target_rows": 2000, "percent": 100, "match": True},
}


class FakeCdcAgent:
    """요청을 기록하고 작업 수명 주기를 흉내 냅니다. 상태는 인스턴스에만 있습니다."""

    def __init__(self, *, polls_to_streaming: int = 2, cutover_ms: float = 1200, verification: str = "match"):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.cutover_requests: list[dict[str, Any]] = []
        self._polls_to_streaming = polls_to_streaming
        self._cutover_ms = cutover_ms
        self._verification = verification
        self.transport = httpx.MockTransport(self.handle)

    # -- 작업 모양 ---------------------------------------------------------------

    @staticmethod
    def _snapshot_job(job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "state": "running",
            "phase": "snapshot",
            "progress": 30,
            "message": "초기 스냅샷 복사 중",
            "polls": 0,
            "metrics": {
                "copy_percent": 30,
                "replication_lag_bytes": 1048576,
                "mismatch": 0,
                "failed_requests": 0,
                "tables": copy.deepcopy(_TABLES_SNAPSHOT),
            },
        }

    @staticmethod
    def _streaming_job(job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "state": "running",
            "phase": "streaming",
            "progress": 100,
            "message": "논리 복제 스트리밍 중",
            "polls": 0,
            "metrics": {
                "copy_percent": 100,
                "replication_lag_bytes": 0,
                "mismatch": 0,
                "failed_requests": 0,
                "cutover_ready": True,
                "tables": copy.deepcopy(_TABLES_STREAMING),
            },
        }

    def _to_streaming(self, job: dict[str, Any]) -> None:
        job.update(phase="streaming", progress=100, message="논리 복제 스트리밍 중")
        job["metrics"].update(copy_percent=100, replication_lag_bytes=0, cutover_ready=True)
        job["metrics"]["tables"] = copy.deepcopy(_TABLES_STREAMING)

    def _to_switched(self, job: dict[str, Any]) -> None:
        job.update(phase="stabilizing", state="succeeded", progress=100, message="전환 완료, 목적지에서 서비스 중")
        job.setdefault("metrics", {}).update(
            traffic_mode="TARGET",
            cutover_ms=self._cutover_ms,
            verification=self._verification,
            failed_requests=0,
            copy_percent=100,
            replication_lag_bytes=0,
        )

    # -- HTTP ----------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else None

        if method == "GET" and path == "/status":
            return httpx.Response(200, json={"status": "ok", "kafka": True, "connect": True, "version": "test"})
        if method == "POST" and path == "/probe":
            return httpx.Response(200, json=[{**e, "ok": True, "error": None} for e in body or []])
        if method == "GET" and path == "/jobs":
            return httpx.Response(200, json=list(self.jobs.values()))
        if method == "POST" and path == "/jobs":
            body = body or {}
            self.created.append(body)
            job_id = body.get("job_id", "job")
            job = self.jobs.setdefault(job_id, self._snapshot_job(job_id))
            return httpx.Response(200, json=job)
        if path.startswith("/jobs/"):
            return self._job_route(method, path, body)
        return httpx.Response(404, json={"detail": f"{path} not found"})

    def _job_route(self, method: str, path: str, body: Any) -> httpx.Response:
        parts = path.strip("/").split("/")
        job_id, sub = parts[1], (parts[2] if len(parts) > 2 else None)

        if sub is None and method == "GET":
            job = self.jobs.setdefault(job_id, self._streaming_job(job_id))
            job["polls"] = job.get("polls", 0) + 1
            if job.get("cutover_requested"):
                self._to_switched(job)
            elif job["phase"] == "snapshot" and job["polls"] >= self._polls_to_streaming:
                self._to_streaming(job)
            return httpx.Response(200, json=job)
        if sub == "cutover" and method == "POST":
            self.cutover_requests.append({"job_id": job_id, **(body or {})})
            job = self.jobs.setdefault(job_id, self._streaming_job(job_id))
            job.update(cutover_requested=True, phase="switching", state="running", progress=75, message="전환 중")
            return httpx.Response(202, json=job)
        if sub == "abort" and method == "POST":
            if job_id not in self.jobs:
                return httpx.Response(404, json={"detail": "not found"})
            self.jobs[job_id].update(state="aborted", message="작업 중단")
            return httpx.Response(200, json=self.jobs[job_id])
        if sub in ("close", "retry") and method == "POST":
            return httpx.Response(200, json={"status": "ok", "job_id": job_id})
        if sub == "secrets" and method == "PUT":
            return httpx.Response(200, json={"status": "ok", "job_id": job_id})
        return httpx.Response(404, json={"detail": f"{path} not found"})

    # -- 설치 ----------------------------------------------------------------------

    def install(self, monkeypatch, url: str = FAKE_AGENT_URL) -> "FakeCdcAgent":
        """CDC_AGENT_URL 을 실제처럼 보이는 주소로 두고, workflows 가 만드는 클라이언트가 이 전송 계층을 쓰게 합니다.

        workflows 는 함수 안에서 client 모듈의 CdcAgentClient 를 가져오므로 그 경계만 바꿉니다 (db_migration 은 고치지 않습니다).
        """
        from app.features.db_migration import client as client_module

        real_client = client_module.CdcAgentClient
        monkeypatch.setenv("CDC_AGENT_URL", url)
        monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)
        monkeypatch.setattr(
            client_module,
            "CdcAgentClient",
            lambda settings, transport=None: real_client(settings, transport=self.transport),
        )
        return self
