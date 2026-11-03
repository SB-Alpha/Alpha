"""workflows API. frontend 가 run 을 만들고, 상태를 보고, 승인·중단·수동 입력을 보냅니다."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.core.config import get_settings

from .engine import Engine, InvalidTransition
from .schemas import (
    AbortRequest,
    ApproveRequest,
    ManualStageInput,
    MigrationRequest,
    RunStatus,
    StageName,
)
from .store import RunNotFound, RunStore

router = APIRouter(prefix="/runs", tags=["workflows"])


@lru_cache
def get_engine() -> Engine:
    # 모듈이 연결되면 여기서 runners={StageName.PLANNING: ..., ...} 로 등록합니다.
    return Engine(RunStore(get_settings().workflows_db_path))


def _apply(run_id: str, action: Callable[[], RunStatus]) -> RunStatus:
    try:
        return action()
    except RunNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} 이(가) 없습니다") from None
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("", response_model=RunStatus, status_code=status.HTTP_201_CREATED)
def create_run(
    req: MigrationRequest, background: BackgroundTasks, engine: Engine = Depends(get_engine)
) -> RunStatus:
    run = engine.create_run(req)
    background.add_task(engine.advance, run.run_id)
    return run


@router.get("", response_model=list[RunStatus])
def list_runs(
    limit: int = Query(50, ge=1, le=200), engine: Engine = Depends(get_engine)
) -> list[RunStatus]:
    return engine.list(limit)


@router.get("/{run_id}", response_model=RunStatus)
def get_run(run_id: str, engine: Engine = Depends(get_engine)) -> RunStatus:
    return _apply(run_id, lambda: engine.get(run_id))


@router.post("/{run_id}/approve", response_model=RunStatus)
def approve_run(
    run_id: str,
    req: ApproveRequest,
    background: BackgroundTasks,
    engine: Engine = Depends(get_engine),
) -> RunStatus:
    run = _apply(run_id, lambda: engine.approve(run_id, req.gate, req.comment))
    background.add_task(engine.advance, run_id)
    return run


@router.post("/{run_id}/abort", response_model=RunStatus)
def abort_run(
    run_id: str, req: AbortRequest | None = None, engine: Engine = Depends(get_engine)
) -> RunStatus:
    reason = req.reason if req else None
    return _apply(run_id, lambda: engine.abort(run_id, reason))


@router.post("/{run_id}/resume", response_model=RunStatus)
def resume_run(run_id: str, background: BackgroundTasks, engine: Engine = Depends(get_engine)) -> RunStatus:
    run = _apply(run_id, lambda: engine.resume(run_id))
    background.add_task(engine.advance, run_id)
    return run


@router.post("/{run_id}/stages/{stage}/manual", response_model=RunStatus)
def submit_manual(
    run_id: str,
    stage: StageName,
    req: ManualStageInput,
    background: BackgroundTasks,
    engine: Engine = Depends(get_engine),
) -> RunStatus:
    run = _apply(run_id, lambda: engine.submit_manual(run_id, stage, req))
    background.add_task(engine.advance, run_id)
    return run
