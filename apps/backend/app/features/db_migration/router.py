"""db_migration API. Forwards to the CDC agent and passes its answers through."""

from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.features.db_migration import input_secrets
from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient
from app.features.db_migration.schemas import CutoverRequest, JobRequest

router = APIRouter(prefix="/db-migration", tags=["db_migration"])
router.include_router(input_secrets.router)


@lru_cache
def _client() -> CdcAgentClient:
    return CdcAgentClient(AgentSettings.from_env())


def get_client() -> CdcAgentClient:
    try:
        return _client()
    except KeyError:
        raise HTTPException(503, "CDC agent is not configured (CDC_AGENT_URL)") from None


Agent = Annotated[CdcAgentClient, Depends(get_client)]


def add_exception_handlers(app: FastAPI) -> None:
    """Agent errors keep their status and detail; network errors become 502."""

    @app.exception_handler(AgentError)
    def agent_error(_: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(httpx.HTTPError)
    def agent_unreachable(_: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse({"detail": f"CDC agent unreachable: {exc}"}, status_code=502)


@router.get("/status")
def status(client: Agent) -> dict:
    """Kafka, Connect and plugin readiness of the CDC cluster, plus job summaries."""
    return client.status()


@router.post("/jobs", status_code=202)
def create_job(req: JobRequest, client: Agent) -> dict:
    """Start a migration job. The same request for an existing job_id returns that job."""
    return client.create_job(req.model_dump())


@router.get("/jobs/{job_id}")
def get_job(job_id: str, client: Agent) -> dict:
    """State, progress, live metrics and evidence of one job."""
    return client.get_job(job_id)


@router.post("/jobs/{job_id}/cutover", status_code=202)
def cutover(job_id: str, req: CutoverRequest, client: Agent) -> dict:
    """Approve the write cutover. Allowed only while the job waits for approval."""
    return client.cutover(job_id, req.approved_by)


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry(job_id: str, client: Agent) -> dict:
    return client.retry(job_id)


@router.post("/jobs/{job_id}/close")
def close(job_id: str, client: Agent) -> dict:
    """Remove connectors and the replication slot after the cutover."""
    return client.close(job_id)


@router.post("/jobs/{job_id}/abort")
def abort(job_id: str, client: Agent) -> dict:
    return client.abort(job_id)
