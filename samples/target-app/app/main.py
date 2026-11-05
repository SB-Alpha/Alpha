"""Sample target service: work-item CRUD on PostgreSQL.

This is the service the platform migrates (AWS ECS Fargate + RDS -> GCP).
No login, cache, queue, or file upload by design.
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import db

SERVED_BY = os.getenv("APP_ENV", "local")
STATIC_DIR = Path(__file__).resolve().parent / "static"
COLUMNS = "id, title, description, status, created_at, updated_at"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
log = logging.getLogger("target-app")

Status = Literal["todo", "in_progress", "done"]
NO_NUL = r"^[^\x00]*$"  # PostgreSQL text cannot store NUL; reject it as 422 instead of a DB 500


class WorkItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, pattern=NO_NUL)
    description: str = Field(default="", max_length=2000, pattern=NO_NUL)
    status: Status = "todo"


class WorkItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200, pattern=NO_NUL)
    description: str | None = Field(default=None, max_length=2000, pattern=NO_NUL)
    status: Status | None = None


class WorkItem(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    status: Status
    created_at: datetime
    updated_at: datetime


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open_pool()
    db.apply_schema()
    yield
    db.close_pool()


app = FastAPI(title="target-app", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_log(request: Request, call_next):
    """One JSON line per request: who served it, what, result, latency."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    finally:
        log.info(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "served_by": SERVED_BY,
        }))
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Served-By"] = SERVED_BY
    return response


@app.get("/health/live", tags=["health"])
def live() -> dict:
    """Process liveness only; does not touch the DB."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready(response: Response) -> dict:
    """Ready when the DB answers. Use this for the load balancer / ECS health check."""
    try:
        with db.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - report any DB failure as not ready
        log.warning(json.dumps({"event": "ready_check_failed", "error": str(exc)[:200]}))
        response.status_code = 503
        return {"status": "unavailable", "db": "error"}
    return {"status": "ok", "db": "ok"}


@app.get("/api/work-items", response_model=list[WorkItem], tags=["work-items"])
def list_items(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with db.pool.connection() as conn:
        return conn.execute(
            f"SELECT {COLUMNS} FROM work_items ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()


@app.get("/api/work-items/{item_id}", response_model=WorkItem, tags=["work-items"])
def get_item(item_id: uuid.UUID):
    with db.pool.connection() as conn:
        row = conn.execute(f"SELECT {COLUMNS} FROM work_items WHERE id = %s", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return row


@app.post("/api/work-items", response_model=WorkItem, status_code=201, tags=["work-items"])
def create_item(body: WorkItemCreate):
    with db.pool.connection() as conn:
        return conn.execute(
            f"INSERT INTO work_items (title, description, status) VALUES (%s, %s, %s) RETURNING {COLUMNS}",
            (body.title, body.description, body.status),
        ).fetchone()


@app.patch("/api/work-items/{item_id}", response_model=WorkItem, tags=["work-items"])
def update_item(item_id: uuid.UUID, body: WorkItemUpdate):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="no fields to update")
    # Column names come from the Pydantic model, never from user input.
    assignments = ", ".join(f"{column} = %s" for column in changes)
    with db.pool.connection() as conn:
        row = conn.execute(
            f"UPDATE work_items SET {assignments}, updated_at = now() WHERE id = %s RETURNING {COLUMNS}",
            (*changes.values(), item_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return row


@app.delete("/api/work-items/{item_id}", status_code=204, tags=["work-items"])
def delete_item(item_id: uuid.UUID) -> Response:
    with db.pool.connection() as conn:
        deleted = conn.execute("DELETE FROM work_items WHERE id = %s", (item_id,)).rowcount
    if deleted == 0:
        raise HTTPException(status_code=404, detail="work item not found")
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
