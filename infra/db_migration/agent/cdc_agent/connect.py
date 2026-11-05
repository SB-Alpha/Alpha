"""Kafka Connect REST client and connector template rendering."""

import json
import time
from pathlib import Path
from string import Template

import httpx


def render(template_path: Path, values: dict[str, str]) -> dict:
    """Fill ${UPPER} slots. Connect's own ${topic}, ${file:...} and $1 are left as is."""
    text = Template(template_path.read_text()).safe_substitute(values)
    return json.loads(text)


# Connect answers 409 while the workers rebalance, for a few seconds up to a session timeout.
REBALANCE_WAIT_S = 60


class ConnectClient:
    def __init__(self, base_url: str):
        self._http = httpx.Client(base_url=base_url, timeout=15)

    def _wait_out_rebalance(self, method: str, path: str, **kwargs) -> httpx.Response:
        deadline = time.monotonic() + REBALANCE_WAIT_S
        while True:
            resp = self._http.request(method, path, **kwargs)
            if resp.status_code != 409 or time.monotonic() > deadline:
                return resp
            time.sleep(2)

    def put_config(self, name: str, config: dict) -> None:
        """Create or update; PUT is idempotent so a retried setup step is safe."""
        self._wait_out_rebalance("PUT", f"/connectors/{name}/config", json=config).raise_for_status()

    def status(self, name: str) -> dict | None:
        resp = self._http.get(f"/connectors/{name}/status")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def pause(self, name: str) -> None:
        self._http.put(f"/connectors/{name}/pause").raise_for_status()

    def resume(self, name: str) -> None:
        self._http.put(f"/connectors/{name}/resume").raise_for_status()

    def restart_failed(self, name: str) -> None:
        """A FAILED task stays failed until restarted. A connector not made yet is fine."""
        resp = self._wait_out_rebalance("POST", f"/connectors/{name}/restart",
                                        params={"includeTasks": "true", "onlyFailed": "true"})
        if resp.status_code != 404:
            resp.raise_for_status()

    def delete(self, name: str) -> None:
        resp = self._wait_out_rebalance("DELETE", f"/connectors/{name}")
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def plugins(self) -> list[str]:
        resp = self._http.get("/connector-plugins")
        resp.raise_for_status()
        return [p["class"] for p in resp.json()]

    def workers_ok(self) -> bool:
        try:
            return self._http.get("/").status_code == 200
        except httpx.HTTPError:
            return False


def connector_running(status: dict | None) -> bool:
    if not status or status["connector"]["state"] != "RUNNING":
        return False
    return bool(status["tasks"]) and all(t["state"] == "RUNNING" for t in status["tasks"])


def connector_error(status: dict | None) -> str | None:
    if not status:
        return None
    if status["connector"]["state"] == "FAILED":
        return (status["connector"].get("trace") or "connector failed").splitlines()[0]
    for task in status.get("tasks", []):
        if task["state"] == "FAILED":
            return (task.get("trace") or "task failed").splitlines()[0]
    return None
