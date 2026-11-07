"""workflows API. frontend 가 run 을 만들고, 상태를 보고, 승인·중단·수동 입력을 보냅니다."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.core.config import get_settings
from app.features.db_migration.guide_workflow import to_job_id

from .engine import Engine, InvalidTransition
from .healer import SelfHealingDiagnosis, diagnose_run
from .reasoning_bank import default_bank
from .preflight import evaluate_cutover
from .readiness import cutover_readiness
from .schemas import (
    AbortRequest,
    ApproveRequest,
    ManualStageInput,
    MigrationRequest,
    PreflightReport,
    RunDetail,
    RunStatus,
    StageName,
    WorkflowState,
)
from .runners import RUNNERS, clean_up_cdc, decommission_cdc
from .store import RunNotFound, RunStore
from .timings import RunTimings, TimingBaseline, run_timings, timing_baseline
from .target_adapters import UnsupportedTarget, adapter_for
from app.core.local_access import require_local_access

router = APIRouter(prefix="/runs", tags=["workflows"])
logger = logging.getLogger(__name__)

PreflightEvaluator = Callable[[RunStatus], PreflightReport]


@lru_cache
def get_engine() -> Engine:
    # 연결된 모듈은 runners.RUNNERS 에 단계별로 등록합니다. 없는 단계는 수동 입력 대기입니다.
    return Engine(
        RunStore(get_settings().workflows_db_path),
        RUNNERS,
        abort_cleanup=clean_up_cdc,
        decommission=decommission_cdc,
        memory=_memory_or_none(),
    )


def _memory_or_none():
    try:
        return default_bank()
    except Exception:  # 기억 저장소를 못 열어도 workflows 는 그대로 돕니다
        logger.warning("ReasoningBank 를 열지 못해 학습 없이 시작합니다", exc_info=True)
        return None


def get_preflight() -> PreflightEvaluator:
    return evaluate_cutover


def _waiting_at_cutover(run: RunStatus) -> bool:
    return run.state == WorkflowState.WAITING_APPROVAL and run.waiting_gate == "cutover"


def _detail(run: RunStatus) -> RunDetail:
    readiness = cutover_readiness(run) if _waiting_at_cutover(run) else None
    return RunDetail(**run.model_dump(), cutover_readiness=readiness, cdc_job_id=to_job_id(run.run_id))


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
    try:
        adapter_for(req)
    except UnsupportedTarget as exc:
        raise HTTPException(422, str(exc)) from None
    run = engine.create_run(req)
    background.add_task(engine.advance, run.run_id)
    return run


@router.get("", response_model=list[RunStatus])
def list_runs(
    limit: int = Query(50, ge=1, le=200), engine: Engine = Depends(get_engine)
) -> list[RunStatus]:
    return engine.list(limit)


@router.get("/stats/timings", response_model=TimingBaseline)
def get_timing_baseline(
    limit: int = Query(200, ge=1, le=200), engine: Engine = Depends(get_engine)
) -> TimingBaseline:
    """지난 실행들의 단계별 소요 시간 분포(최소·중앙값·최대, 표본 수). 예상이 아니라 과거 실측입니다."""
    return timing_baseline(engine.list(limit))


@router.get("/{run_id}/timings", response_model=RunTimings)
def get_run_timings(run_id: str, engine: Engine = Depends(get_engine)) -> RunTimings:
    """이 run 의 단계별 실제 소요 시간. 재지 못한 단계는 값 없이 이유(note)만 돌려줍니다."""
    try:
        return run_timings(engine.get(run_id))
    except RunNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} 이(가) 없습니다") from None


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
    # 승인 조건은 저장된 값으로 서버가 검사합니다. 실시간 점검(GET /preflight)은 참고용이고,
    # 승인 요청이 프록시 제한(30초) 안에 끝나도록 여기서는 네트워크를 부르지 않습니다.
    run = _apply(
        run_id,
        lambda: engine.approve(
            run_id,
            req.gate,
            req.comment,
            approver=req.approver,
            preflight_verdict=req.preflight_verdict,
            preflight_measured_at=req.preflight_measured_at,
            acknowledge_unknown=req.acknowledge_unknown,
        ),
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


@router.post("/{run_id}/decommission", response_model=RunDetail, dependencies=[Depends(require_local_access)])
def decommission_run(
    run_id: str,
    background: BackgroundTasks,
    operator: str | None = Query(None, description="철거 승인자 식별자"),
    engine: Engine = Depends(get_engine),
) -> RunDetail:
    """전환이 끝난 run 의 이전 자원(CDC 복제 작업·복제 슬롯·VPN)을 정리합니다. 실패하면 다시 부를 수 있습니다.

    VPN 삭제가 몇 분 걸려 백그라운드에서 돌립니다. 진행과 결과는 cleanup_state · cleanup_message 에 남습니다.
    비가역 조치이므로 로컬 권한(require_local_access)과 데이터 검증(match) 상태를 확인합니다.
    """
    run = _apply(run_id, lambda: engine.start_decommission(run_id, operator=operator))
    background.add_task(engine.run_decommission, run_id)
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


@router.get("/{run_id}/preflight", response_model=PreflightReport)
def get_preflight_report(
    run_id: str,
    engine: Engine = Depends(get_engine),
    preflight: PreflightEvaluator = Depends(get_preflight),
) -> PreflightReport:
    """전환 승인 ② 대기 중인 run 의 전환 전 점검 결과를 돌려줍니다. 참고용이며 승인을 막지 않습니다."""
    run = _apply(run_id, lambda: engine.get(run_id))
    if not _waiting_at_cutover(run):
        raise HTTPException(status.HTTP_409_CONFLICT, f"전환 승인 ② 대기 중인 run 만 점검합니다 (현재 {run.state.value})")
    return preflight(run)


@router.get("/{run_id}/diagnosis", response_model=SelfHealingDiagnosis)
def get_diagnosis(run_id: str, engine: Engine = Depends(get_engine)) -> SelfHealingDiagnosis:
    """장애 발생 시 자가치유(Self-Healing) 진단 리포트를 돌려줍니다. 과거에 풀린 같은 장애면 기억으로 답합니다."""
    return _diagnose(run_id, engine, use_memory=True)


@router.post("/{run_id}/diagnose", response_model=SelfHealingDiagnosis)
def rediagnose(run_id: str, engine: Engine = Depends(get_engine)) -> SelfHealingDiagnosis:
    """기억을 건너뛰고 새로 진단합니다 (AI 재진단)."""
    return _diagnose(run_id, engine, use_memory=False)


def _diagnose(run_id: str, engine: Engine, *, use_memory: bool) -> SelfHealingDiagnosis:
    run = _apply(run_id, lambda: engine.get(run_id))
    if run.state != WorkflowState.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"실패한 run 만 진단합니다 (현재 {run.state.value})")
    return diagnose_run(run, use_memory=use_memory)
