"""Client for the Cloudflare Worker traffic-control API."""

import time

import httpx

from cdc_agent.config import Settings


class WorkerClient:
    def __init__(self, settings: Settings):
        if not settings.worker_url:
            raise RuntimeError("WORKER_URL is not set; cutover needs the traffic-control API")
        headers = {}
        if settings.worker_token_file:
            headers["Authorization"] = f"Bearer {settings.worker_token_file.read_text().strip()}"
        self._http = httpx.Client(base_url=settings.worker_url, headers=headers, timeout=5)

    def _post(self, path: str, body: dict) -> dict:
        resp = self._http.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def hold(self, run_id: str, max_hold_ms: int) -> dict:
        return self._post("/control/hold", {"run_id": run_id, "max_hold_ms": max_hold_ms})

    def switch(self, run_id: str) -> dict:
        return self._post("/control/switch", {"run_id": run_id, "to": "TARGET"})

    def release(self, run_id: str) -> dict:
        return self._post("/control/release", {"run_id": run_id})

    def status(self) -> dict:
        resp = self._http.get("/control/status")
        resp.raise_for_status()
        return resp.json()

    def wait_inflight_zero(self, timeout_s: float) -> dict:
        """Requests already sent to AWS before the hold must finish before writes are blocked."""
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.status()
            if status.get("inflight_source", 0) == 0:
                return status
            if time.monotonic() > deadline:
                raise TimeoutError(f"inflight_source still {status.get('inflight_source')} after {timeout_s}s")
            time.sleep(0.02)
