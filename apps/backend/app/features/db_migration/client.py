"""Client for the CDC control agent (infra/db_migration/agent).

The agent sits behind Cloudflare Tunnel + Access, so every call carries the
Access service-token headers. Long work runs on the agent; callers poll get_job.
"""

import os
import time
from dataclasses import dataclass, field

import httpx

from .settings import required, tunables


@dataclass(frozen=True)
class AgentSettings:
    url: str
    access_client_id: str | None = None
    access_client_secret: str | None = None
    timeout_s: float = field(default_factory=lambda: tunables().agent_timeout_s)
    abort_timeout_s: float = field(default_factory=lambda: tunables().agent_abort_timeout_s)

    @property
    def is_simulation(self) -> bool:
        return self.url.startswith("mock")

    @classmethod
    def from_env(cls) -> "AgentSettings":
        url = os.getenv("CDC_AGENT_URL")
        if not url:
            if os.getenv("ALPHA_CDC_SIMULATION", "0") in ("1", "true", "True"):
                url = "mock://simulation"
            else:
                url = required("CDC_AGENT_URL")
        return cls(
            url=url,
            access_client_id=os.getenv("CDC_AGENT_ACCESS_CLIENT_ID"),
            access_client_secret=os.getenv("CDC_AGENT_ACCESS_CLIENT_SECRET"),
        )


class AgentError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"agent returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class _MockSimulationTransport(httpx.BaseTransport):
    """Local simulation transport for CDC Agent when OCI VPN or agent is disconnected (Issue #35)."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._cutover_triggered: set[str] = set()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import json
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/status":
            return httpx.Response(200, json={"status": "ok", "mode": "simulation", "version": "1.0.0-mock"})

        if method == "POST" and path == "/probe":
            try:
                endpoints = json.loads(request.content.decode("utf-8"))
            except Exception:
                endpoints = []
            res = [{"host": e.get("host"), "port": e.get("port"), "ok": True, "error": None} for e in endpoints]
            return httpx.Response(200, json=res)

        if method == "POST" and path == "/jobs":
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            job_id = body.get("job_id", "sim-job")
            if job_id not in self._jobs:
                self._jobs[job_id] = {
                    "job_id": job_id,
                    "state": "running",
                    "phase": "snapshot",
                    "progress": 30,
                    "message": "[Simulation] Initial table snapshot in progress",
                    "created_at": time.time(),
                    "polls": 0,
                    "metrics": {
                        "simulated": True,
                        "copy_percent": 30,
                        "replication_lag_bytes": 1048576,
                        "mismatch": 0,
                        "failed_requests": 0,
                        "tables": {
                            "users": {"source_rows": 500, "target_rows": 150, "percent": 30, "match": False},
                            "orders": {"source_rows": 2000, "target_rows": 600, "percent": 30, "match": False},
                        },
                    },
                }
            return httpx.Response(200, json=self._jobs[job_id])

        if path.startswith("/jobs/"):
            parts = path.strip("/").split("/")
            job_id = parts[1]
            sub = parts[2] if len(parts) > 2 else None

            if sub == "cutover" and method == "POST":
                self._cutover_triggered.add(job_id)
                job = self._jobs.setdefault(job_id, {"job_id": job_id})
                job["phase"] = "switching"
                job["state"] = "running"
                job["progress"] = 75
                job["message"] = "[Simulation] L7 traffic hold -> switch in progress"
                return httpx.Response(202, json=job)

            if sub == "abort" and method == "POST":
                # 추적은 aborted 를 봐야 끝납니다. 실제 에이전트처럼 없는 작업은 404, 있으면 멈춘 작업을 돌려줍니다.
                if job_id not in self._jobs:
                    return httpx.Response(404, json={"detail": "not found"})
                self._jobs[job_id].update(state="aborted", message="[Simulation] Job aborted")
                return httpx.Response(200, json=self._jobs[job_id])

            if sub in ("close", "retry") and method == "POST":
                return httpx.Response(200, json={"status": "ok", "job_id": job_id})

            if sub == "secrets" and method == "PUT":
                return httpx.Response(200, json={"status": "ok", "job_id": job_id})

            if sub is None and method == "GET":
                job = self._jobs.setdefault(job_id, {
                    "job_id": job_id,
                    "state": "running",
                    "phase": "streaming",
                    "progress": 100,
                    "message": "[Simulation] CDC logical replication streaming active",
                    "created_at": time.time(),
                    "polls": 0,
                    "metrics": {
                        "simulated": True,
                        "copy_percent": 100,
                        "replication_lag_bytes": 0,
                        "mismatch": 0,
                        "failed_requests": 0,
                        "cutover_ready": True,
                        "tables": {
                            "users": {"source_rows": 500, "target_rows": 500, "percent": 100, "match": True},
                            "orders": {"source_rows": 2000, "target_rows": 2000, "percent": 100, "match": True},
                        },
                    },
                })
                job["polls"] = job.get("polls", 0) + 1

                if job_id in self._cutover_triggered:
                    job["phase"] = "stabilizing"
                    job["state"] = "succeeded"
                    job["progress"] = 100
                    job["message"] = "[Simulation] Cutover completed. Traffic serving on GCP Cloud Run."
                    m = job.setdefault("metrics", {})
                    # 실제 에이전트(jobs.py)와 같은 키로 보고합니다.
                    m["simulated"] = True
                    m["traffic_mode"] = "TARGET"
                    m["cutover_ms"] = 1200
                    m["verification"] = "match"
                    m["failed_requests"] = 0
                    m["copy_percent"] = 100
                    m["replication_lag_bytes"] = 0
                elif job.get("polls", 0) >= 2 and job["phase"] == "snapshot":
                    job["phase"] = "streaming"
                    job["progress"] = 100
                    job["message"] = "[Simulation] CDC logical replication streaming active"
                    m = job.setdefault("metrics", {})
                    m["copy_percent"] = 100
                    m["replication_lag_bytes"] = 0
                    m["cutover_ready"] = True
                    if "tables" in m:
                        m["tables"]["users"] = {"source_rows": 500, "target_rows": 500, "percent": 100, "match": True}
                        m["tables"]["orders"] = {"source_rows": 2000, "target_rows": 2000, "percent": 100, "match": True}

                return httpx.Response(200, json=job)

        return httpx.Response(404, json={"detail": f"Path {path} not found in simulation"})


# db_migration 과 cutover 가 클라이언트를 따로 만들어도 같은 모의 job 을 보도록 하나만 둡니다.
_SHARED_SIMULATION = _MockSimulationTransport()


class CdcAgentClient:
    def __init__(self, settings: AgentSettings, transport: httpx.BaseTransport | None = None):
        # 실제 URL 이 있으면 ALPHA_CDC_SIMULATION 이 켜져 있어도 실제 에이전트를 부릅니다.
        self._is_simulated = settings.is_simulation
        if self._is_simulated and transport is None:
            transport = _SHARED_SIMULATION

        headers = {}
        if settings.access_client_id and settings.access_client_secret:
            headers["CF-Access-Client-Id"] = settings.access_client_id
            headers["CF-Access-Client-Secret"] = settings.access_client_secret

        base_url = "http://mock-simulation" if self._is_simulated else settings.url
        self._http = httpx.Client(base_url=base_url, headers=headers,
                                  timeout=settings.timeout_s, transport=transport)
        self._cleanup_timeout_s = settings.abort_timeout_s

    def _call(self, method: str, path: str, body: dict | list | None = None, timeout_s: float | None = None) -> dict | list:
        kwargs = {} if timeout_s is None else {"timeout": timeout_s}
        resp = self._http.request(method, path, json=body, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise AgentError(resp.status_code, str(detail))
        return resp.json()

    def status(self) -> dict:
        return self._call("GET", "/status")

    def create_job(self, request: dict) -> dict:
        """Idempotent: the same request for an existing job_id returns that job."""
        return self._call("POST", "/jobs", request)

    def get_job(self, job_id: str) -> dict:
        return self._call("GET", f"/jobs/{job_id}")

    def list_jobs(self) -> list[dict]:
        return self._call("GET", "/jobs")

    def cutover(self, job_id: str, approved_by: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/cutover", {"approved_by": approved_by})

    def retry(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/retry")

    # close and abort clean up on the agent before answering, which takes longer than other calls.
    def close(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/close", timeout_s=self._cleanup_timeout_s)

    def abort(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/abort", timeout_s=self._cleanup_timeout_s)

    def put_secrets(self, job_id: str, values: dict[str, str]) -> dict:
        return self._call("PUT", f"/jobs/{job_id}/secrets", values)

    def probe(self, endpoints: list[dict]) -> list[dict]:
        """TCP check from the CDC nodes: [{host, port}] -> [{host, port, ok, error}]."""
        return self._call("POST", "/probe", endpoints)

    def catalog(self, query: dict) -> dict:
        """Transient DB account for catalog reads only. Never echo a remote error containing the body."""
        if self._is_simulated:
            raise AgentError(409, "catalog discovery requires a real agent; simulation is not implemented")
        try:
            return self._call("POST", "/catalog", query)
        except AgentError as exc:
            raise AgentError(exc.status_code, "PostgreSQL catalog request failed") from None
        except (httpx.HTTPError, ValueError):
            raise AgentError(502, "PostgreSQL catalog request failed") from None

    def wait_for(self, job_id: str, phases: set[str], timeout_s: float, interval_s: float = 3) -> dict:
        """Poll until the job reaches one of the phases. A failed job raises."""
        deadline = time.monotonic() + timeout_s
        while True:
            job = self.get_job(job_id)
            if job["phase"] in phases:
                return job
            if job["state"] == "failed":
                raise AgentError(409, f"job {job_id} failed in {job['phase']}: {job['message']}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {job['state']}/{job['phase']} after {timeout_s}s")
            time.sleep(interval_s)
