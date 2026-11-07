"""db_migration API. /runs is the module's own procedure; /jobs forwards to the CDC agent."""

from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from app.core.local_access import require_local_access
from app.features.db_migration import db_workflow, input_secrets
from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient
from app.features.db_migration.schemas import (
    RUN_ID_PATTERN,
    CutoverRequest,
    CutoverResult,
    FinishResult,
    JobRequest,
    RunStatus,
)
from app.features.db_migration.settings import MissingSetting

router = APIRouter(prefix="/db-migration", tags=["db_migration"], dependencies=[Depends(require_local_access)])
router.include_router(input_secrets.router)


@lru_cache
def _client() -> CdcAgentClient:
    return CdcAgentClient(AgentSettings.from_env())


def get_client() -> CdcAgentClient:
    try:
        return _client()
    except MissingSetting as exc:
        raise HTTPException(503, str(exc)) from None


Agent = Annotated[CdcAgentClient, Depends(get_client)]
RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]


async def _json_body(request: Request) -> Any:
    # Read by hand: FastAPI's own validation errors quote the submitted value back.
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(422, "body must be a JSON object") from None


def add_exception_handlers(app: FastAPI) -> None:
    """Agent errors keep their status and detail; network errors become 502."""

    @app.exception_handler(AgentError)
    def agent_error(_: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(httpx.HTTPError)
    def agent_unreachable(_: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse({"detail": f"CDC agent unreachable: {exc}"}, status_code=502)

    @app.exception_handler(MissingSetting)
    def missing_setting(_: Request, exc: MissingSetting) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(db_workflow.InvalidInput)
    def invalid_input(_: Request, exc: db_workflow.InvalidInput) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "fields": exc.fields}, status_code=422)

    @app.exception_handler(db_workflow.NotAllowed)
    def not_allowed(_: Request, exc: db_workflow.NotAllowed) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(db_workflow.UnknownRun)
    def unknown_run(_: Request, exc: db_workflow.UnknownRun) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)


@router.post("/runs", status_code=202)
def start_run(request: Request, body: Annotated[Any, Depends(_json_body)], client: Agent) -> dict:
    """Save the inputs and start in the background. Follow it with GET /runs/{run_id}."""
    discover = getattr(request.app.state, "discover_source", None)
    return {"run_id": db_workflow.run(body, discover=discover, agent=client)}


@router.get("/runs/{run_id}")
def run_status(run_id: RunId, client: Agent) -> RunStatus:
    return db_workflow.status(run_id, agent=client)


@router.post("/runs/{run_id}/cutover")
def run_cutover(run_id: RunId, req: CutoverRequest, client: Agent) -> CutoverResult:
    """Switch writes and return the outcome. Allowed only at cutover_ready."""
    return db_workflow.cutover(run_id, req.approved_by, agent=client)


@router.post("/runs/{run_id}/finish")
def run_finish(run_id: RunId, client: Agent) -> FinishResult:
    """After the cutover. Long running: it removes the VPN. Call again when incomplete."""
    return db_workflow.finish(run_id, agent=client)


@router.post("/runs/{run_id}/stop")
def run_stop(run_id: RunId, client: Agent) -> FinishResult:
    """Before the cutover. Long running: it removes the VPN. Call again when incomplete."""
    return db_workflow.stop(run_id, agent=client)


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
