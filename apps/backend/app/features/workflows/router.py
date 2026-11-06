"""workflows API. frontend 가 run 을 만들고, 상태를 보고, 승인·중단·수동 입력을 보냅니다."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.core.config import get_settings

from .engine import Engine, InvalidTransition
from .healer import SelfHealingDiagnosis, diagnose_run
from .readiness import cutover_readiness
from .schemas import (
    AbortRequest,
    ApproveRequest,
    ManualStageInput,
    MigrationRequest,
    RunDetail,
    RunStatus,
    StageName,
    WorkflowState,
)
from .runners import RUNNERS, clean_up_cdc
from .store import RunNotFound, RunStore

router = APIRouter(prefix="/runs", tags=["workflows"])


@lru_cache
def get_engine() -> Engine:
    # 연결된 모듈은 runners.RUNNERS 에 단계별로 등록합니다. 없는 단계는 수동 입력 대기입니다.
    return Engine(RunStore(get_settings().workflows_db_path), RUNNERS, abort_cleanup=clean_up_cdc)


def _detail(run: RunStatus) -> RunDetail:
    is_waiting_cutover = run.state == WorkflowState.WAITING_APPROVAL and run.waiting_gate == "cutover"
    readiness = cutover_readiness(run) if is_waiting_cutover else None
    return RunDetail(**run.model_dump(), cutover_readiness=readiness)


def _apply(run_id: str, action: Callable[[], RunStatus]) -> RunDetail:
    try:
        return _detail(action())
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


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, engine: Engine = Depends(get_engine)) -> RunDetail:
    return _apply(run_id, lambda: engine.get(run_id))


@router.post("/{run_id}/approve", response_model=RunDetail)
def approve_run(
    run_id: str,
    req: ApproveRequest,
    background: BackgroundTasks,
    engine: Engine = Depends(get_engine),
) -> RunDetail:
    run = _apply(
        run_id,
        lambda: engine.approve(run_id, req.gate, req.comment, acknowledge_unknown=req.acknowledge_unknown),
    )
    background.add_task(engine.advance, run_id)
    return run


@router.post("/{run_id}/abort", response_model=RunDetail)
def abort_run(
    run_id: str,
    background: BackgroundTasks,
    req: AbortRequest | None = None,
    engine: Engine = Depends(get_engine),
) -> RunDetail:
    reason = req.reason if req else None
    run = _apply(run_id, lambda: engine.abort(run_id, reason))
    background.add_task(engine.run_abort_cleanup, run_id)
    return run


@router.post("/{run_id}/cleanup", response_model=RunDetail)
def retry_cleanup(run_id: str, background: BackgroundTasks, engine: Engine = Depends(get_engine)) -> RunDetail:
    """중단 뒤 정리에 실패한 run 의 외부 자원 정리를 다시 시도합니다."""
    run = _apply(run_id, lambda: engine.retry_cleanup(run_id))
    background.add_task(engine.run_abort_cleanup, run_id)
    return run


@router.post("/{run_id}/resume", response_model=RunDetail)
def resume_run(run_id: str, background: BackgroundTasks, engine: Engine = Depends(get_engine)) -> RunDetail:
    run = _apply(run_id, lambda: engine.resume(run_id))
    background.add_task(engine.advance, run_id)
    return run


@router.post("/{run_id}/stages/{stage}/manual", response_model=RunDetail)
def submit_manual(
    run_id: str,
    stage: StageName,
    req: ManualStageInput,
    background: BackgroundTasks,
    engine: Engine = Depends(get_engine),
) -> RunDetail:
    run = _apply(run_id, lambda: engine.submit_manual(run_id, stage, req))
    background.add_task(engine.advance, run_id)
    return run


@router.get("/{run_id}/diagnosis", response_model=SelfHealingDiagnosis)
@router.post("/{run_id}/diagnose", response_model=SelfHealingDiagnosis)
def get_diagnosis(run_id: str, engine: Engine = Depends(get_engine)) -> SelfHealingDiagnosis:
    """장애 발생 시 AI 자가치유(Self-Healing) 진단 리포트를 생성합니다."""
    run = _apply(run_id, lambda: engine.get(run_id))
    if run.state != WorkflowState.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"실패한 run 만 진단합니다 (현재 {run.state.value})")
    return diagnose_run(run)
