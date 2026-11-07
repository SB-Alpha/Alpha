"""Local stand-in for the Cloudflare Worker (contract: ../agent/WORKER_API.md).

Implements the same control API (hold / switch / release / status) and a tiny
write API that routes to the source or target DB the way the real Worker routes
to AWS or GCP. Held requests wait and are released to next_origin, never failed.
"""

import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

TOKEN = os.environ["WORKER_TOKEN"]
DSN = {"SOURCE": os.environ["SOURCE_DSN"], "TARGET": os.environ["TARGET_DSN"]}
MAX_WAIT_S = 30

app = FastAPI(title="fake-worker")
lock = threading.Lock()
released = threading.Event()
released.set()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_counts(since: str) -> dict:
    return {"since": since, "write_total": 0, "write_failed": 0, "read_total": 0, "read_failed": 0}


state = {"mode": "SOURCE", "next_origin": "SOURCE", "run_id": None, "hold_started_at": None,
         "released_at": None, "held_count": 0, "inflight_source": 0, "updated_at": None,
         "request_counts": new_counts(now())}


def auth(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401)


class Hold(BaseModel):
    run_id: str
    max_hold_ms: int = 3000


class Switch(BaseModel):
    run_id: str
    to: str = "TARGET"


class Release(BaseModel):
    run_id: str


@app.post("/control/hold", dependencies=[Depends(auth)])
def hold(body: Hold) -> dict:
    with lock:
        if state["mode"] == "HOLD" and state["run_id"] == body.run_id:
            return dict(state)
        if state["mode"] != "SOURCE":
            raise HTTPException(409, f"mode is {state['mode']}")
        released.clear()
        started = now()
        state.update(mode="HOLD", next_origin="SOURCE", run_id=body.run_id, hold_started_at=started,
                     released_at=None, held_count=0, updated_at=started, request_counts=new_counts(started))
        return dict(state)


@app.post("/control/switch", dependencies=[Depends(auth)])
def switch(body: Switch) -> dict:
    with lock:
        if state["mode"] != "HOLD" or state["run_id"] != body.run_id:
            raise HTTPException(409, f"mode is {state['mode']} for run {state['run_id']}")
        state.update(next_origin=body.to, updated_at=now())
        return dict(state)


@app.post("/control/release", dependencies=[Depends(auth)])
def release(body: Release) -> dict:
    with lock:
        if state["mode"] != "HOLD":
            return dict(state)
        if state["run_id"] != body.run_id:
            raise HTTPException(409, f"held by run {state['run_id']}")
        state.update(mode=state["next_origin"], released_at=now(), updated_at=now())
        released.set()
        return dict(state)


@app.get("/control/status", dependencies=[Depends(auth)])
def status() -> dict:
    with lock:
        return dict(state)


class Write(BaseModel):
    op: str  # insert | update | delete
    id: uuid.UUID
    title: str = ""
    status: str = "todo"


def route() -> str:
    """Wait while HOLD; return where this request goes. Counts in-flight source requests."""
    with lock:
        held = state["mode"] == "HOLD"
        if held:
            state["held_count"] += 1
    if held and not released.wait(MAX_WAIT_S):
        raise HTTPException(503, "hold exceeded")
    with lock:
        origin = state["mode"]
        if origin == "SOURCE":
            state["inflight_source"] += 1
        return origin


@contextmanager
def routed(*, is_write: bool) -> Iterator[str]:
    """Route one request, then count it when it ends. Any exception counts as a failure, like a 5xx."""
    origin = None
    is_failure = True
    try:
        origin = route()
        yield origin
        is_failure = False
    finally:
        kind = "write" if is_write else "read"
        with lock:
            if origin == "SOURCE":
                state["inflight_source"] -= 1
            state["request_counts"][f"{kind}_total"] += 1
            state["request_counts"][f"{kind}_failed"] += int(is_failure)


@app.post("/api/items")
def write(body: Write) -> dict:
    started = time.monotonic()
    with routed(is_write=True) as origin, psycopg.connect(DSN[origin], autocommit=True, connect_timeout=5) as conn:
        if body.op == "insert":
            conn.execute("INSERT INTO work_items (id, title, status) VALUES (%s, %s, %s)",
                         (body.id, body.title, body.status))
        elif body.op == "update":
            conn.execute("UPDATE work_items SET title = %s, status = %s, updated_at = now() WHERE id = %s",
                         (body.title, body.status, body.id))
        elif body.op == "delete":
            conn.execute("DELETE FROM work_items WHERE id = %s", (body.id,))
        else:
            raise HTTPException(422, "op")
    return {"served_by": origin, "ms": round((time.monotonic() - started) * 1000, 1)}


@app.get("/health/ready")
def ready(response: Response) -> dict:
    """A read through the gate, for the external prober."""
    with routed(is_write=False) as origin, psycopg.connect(DSN[origin], autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT 1")
    response.headers["X-Gate-Origin"] = origin
    return {"served_by": origin}
