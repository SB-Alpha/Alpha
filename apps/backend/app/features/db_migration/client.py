"""Client for the CDC control agent (infra/db_migration/agent).

The agent sits behind Cloudflare Tunnel + Access, so every call carries the
Access service-token headers. Long work runs on the agent; callers poll get_job.
"""

import os
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class AgentSettings:
    url: str
    access_client_id: str | None = None
    access_client_secret: str | None = None
    timeout_s: float = 30

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            url=os.environ["CDC_AGENT_URL"],
            access_client_id=os.getenv("CDC_AGENT_ACCESS_CLIENT_ID"),
            access_client_secret=os.getenv("CDC_AGENT_ACCESS_CLIENT_SECRET"),
        )


class AgentError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"agent returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class CdcAgentClient:
    def __init__(self, settings: AgentSettings, transport: httpx.BaseTransport | None = None):
        headers = {}
        if settings.access_client_id and settings.access_client_secret:
            headers["CF-Access-Client-Id"] = settings.access_client_id
            headers["CF-Access-Client-Secret"] = settings.access_client_secret
        self._http = httpx.Client(base_url=settings.url, headers=headers,
                                  timeout=settings.timeout_s, transport=transport)

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

    def cutover(self, job_id: str, approved_by: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/cutover", {"approved_by": approved_by})

    def retry(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/retry")

    def close(self, job_id: str) -> dict:
        return self._call("POST", f"/jobs/{job_id}/close")

    def abort(self, job_id: str) -> dict:
        # Cleans up on the agent before answering; Cloudflare waits ~100s at most.
        return self._call("POST", f"/jobs/{job_id}/abort", timeout_s=95)

    def put_secrets(self, job_id: str, values: dict[str, str]) -> dict:
        return self._call("PUT", f"/jobs/{job_id}/secrets", values)

    def probe(self, endpoints: list[dict]) -> list[dict]:
        """TCP check from the CDC nodes: [{host, port}] -> [{host, port, ok, error}]."""
        return self._call("POST", "/probe", endpoints)

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
