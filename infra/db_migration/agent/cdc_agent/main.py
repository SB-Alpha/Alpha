"""CDC control agent API. Reached only through Cloudflare Tunnel + Access.

Long work runs in the background; callers poll GET /jobs/{id} (Cloudflare waits ~100s at most).
"""

import logging
import socket
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cdc_agent.config import get_settings
from cdc_agent.jobs import SETUP_PHASES, CloseRefused, JobBusy, Runner, WorkerNotReady
from cdc_agent.models import JOB_ID_PATTERN, CutoverRequest, Job, JobRequest
from cdc_agent.secret_store import SecretsDisabled, SecretStore
from cdc_agent.store import JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cdc_agent")

REQUIRED_PLUGINS = {
    "io.debezium.connector.postgresql.PostgresConnector",
    "io.debezium.connector.jdbc.JdbcSinkConnector",
}

settings = get_settings()
store = JobStore(settings)
secrets = SecretStore(settings, store)
runner = Runner(settings, store, secrets)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(30):
        try:
            store.ensure_topic()
            secrets.ensure_topic()
            break
        except Exception as exc:
            log.warning("kafka not ready (%s), retry %d", exc, attempt + 1)
            time.sleep(2)
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="alpha-cdc-agent", version="0.1.0", lifespan=lifespan)


def load(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "node": settings.node_id}


@app.get("/status")
def status() -> dict:
    connect_ok = runner.connect.workers_ok()
    plugins = set(runner.connect.plugins()) if connect_ok else set()
    try:
        jobs = store.all()
        kafka_ok = True
    except Exception:
        jobs, kafka_ok = {}, False
    return {
        "node": settings.node_id,
        "kafka": kafka_ok,
        "connect": connect_ok,
        "plugins_missing": sorted(REQUIRED_PLUGINS - plugins) if connect_ok else None,
        "worker_configured": runner.worker_configured(),
        "jobs": {j.job_id: {"state": j.state, "phase": j.phase} for j in jobs.values()},
    }


@app.post("/jobs", status_code=202)
def create_job(req: JobRequest) -> Job:
    existing = store.get(req.job_id)
    if existing is not None:
        if existing.request != req:
            raise HTTPException(409, f"job {req.job_id} exists with a different request")
        return existing
    job = Job(job_id=req.job_id, request=req)
    store.put(job)
    return job


@app.get("/jobs")
def list_jobs() -> list[Job]:
    return list(store.all().values())


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Job:
    return runner.refresh_metrics(load(job_id))


@app.post("/jobs/{job_id}/cutover", status_code=202)
def cutover(job_id: str, req: CutoverRequest) -> Job:
    job = load(job_id)
    if job.state != "waiting_approval" or job.phase != "cutover_ready":
        raise HTTPException(409, f"job is {job.state}/{job.phase}, not waiting for cutover approval")
    try:
        runner.check_worker()
    except WorkerNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    runner.start_cutover(job, req.approved_by)
    return job


@app.post("/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str) -> Job:
    job = load(job_id)
    if job.state != "failed" or job.phase not in SETUP_PHASES:
        raise HTTPException(409, "only a failed setup step can be retried")
    try:
        runner.restart_failed_connectors(job)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not restart the connectors: {runner.error_text(job_id, exc)}") from exc
    job.report(state="running", message="재시도")
    store.put(job)
    return job


@app.post("/jobs/{job_id}/close")
def close(job_id: str) -> Job:
    job = load(job_id)
    if job.phase != "stabilizing":
        raise HTTPException(409, "close is allowed after cutover (stabilizing)")
    try:
        runner.close(job)
    except CloseRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    return job


@app.post("/jobs/{job_id}/abort")
def abort(job_id: str) -> Job:
    job = load(job_id)
    # A failed setup can still hold a slot, so it can be aborted to clean up.
    if job.state in ("aborted", "succeeded") or job.phase in ("cutover", "stabilizing", "closed"):
        raise HTTPException(409, "abort is allowed only before cutover")
    try:
        return runner.abort(job_id)
    except JobBusy as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        log.exception("abort %s failed", job_id)
        job = load(job_id)
        error = runner.error_text(job_id, exc)
        job.report(message=f"정리 실패, 다시 중단하세요: {error}")
        store.put(job)
        raise HTTPException(502, f"cleanup failed: {error}") from exc


@app.put("/jobs/{job_id}/secrets")
def put_secrets(job_id: str, values: dict[str, Any]) -> dict:
    """Values never come back in a response or a log. Works before the job exists."""
    if not JOB_ID_PATTERN.match(job_id):
        raise HTTPException(422, "job_id: lowercase letters, digits, underscore; 2-31 chars")
    try:
        secrets.put(job_id, values)
    except SecretsDisabled as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"job_id": job_id, "stored": True}


class ProbeTarget(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)


@app.post("/probe")
def probe(targets: list[ProbeTarget]) -> list[dict]:
    """TCP reachability from this node, to tell a VPN or firewall problem from a DB one."""
    results = []
    for t in targets[:10]:
        try:
            socket.create_connection((t.host, t.port), timeout=3).close()
            results.append({"host": t.host, "port": t.port, "ok": True, "error": None})
        except OSError as exc:
            results.append({"host": t.host, "port": t.port, "ok": False, "error": str(exc)})
    return results
