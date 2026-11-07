"""run 오케스트레이터: 단계 순서 호출, 승인 대기, 수동 입력, 중단.

각 모듈의 기술 구현은 여기서 다시 만들지 않습니다. 단계마다 등록된 runner 가
`run.request` 와 앞 단계 `outputs` 로 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 호출합니다.

- runner 가 등록되지 않은 단계는 `pending` 으로 두고 run 을 `paused`("미구현 — 수동 입력 대기")로 멈춥니다.
  가짜 성공을 만들지 않습니다. 사람이 결과(fixture)를 넣으면 `manual=true` 로 기록하고 이어서 진행합니다.
- 단계가 예외를 내면 그 단계와 run 을 `failed` 로 두고 멈춥니다. 수동 입력으로 이어갈 수 있습니다.
- abort 는 즉시 run 을 `aborted` 로 바꿉니다. 실행 중인 모듈은 다음 report() 때 멈춥니다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from .context import RunAborted, StoreRunContext, now, with_stage
from .readiness import cutover_readiness
from .reasoning_bank import ReasoningBank
from .schemas import (
    GATE_BEFORE,
    STAGE_ORDER,
    ApprovalRecord,
    CleanupState,
    CutoverDecision,
    Gate,
    ManualStageInput,
    MigrationRequest,
    PreflightVerdict,
    RunContext,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from .store import RunStore

logger = logging.getLogger(__name__)

StageRunner = Callable[[RunStatus, RunContext], BaseModel | dict[str, Any]]
# 중단된 run 이 남긴 외부 자원을 정리합니다. 정리할 것이 없으면 False, 실패하면 예외입니다. 다시 불러도 안전해야 합니다.
AbortCleanup = Callable[[RunStatus], bool]

GATE_LABEL: dict[Gate, str] = {"plan": "계획 승인 ①", "cutover": "전환 승인 ②"}

_ADVANCEABLE = {WorkflowState.PENDING, WorkflowState.RUNNING}
_FINISHED = {WorkflowState.SUCCEEDED, WorkflowState.ABORTED}
_MANUAL_ALLOWED = {WorkflowState.PAUSED, WorkflowState.FAILED}
_RESUMABLE = {WorkflowState.FAILED, WorkflowState.RUNNING, WorkflowState.PENDING}
_MAX_ERROR_MESSAGE = 500
_CUTOVER_RETRY_NOTE = "재시도하려면 전환 승인 ② 를 다시 받아야 합니다"
_CUTOVER_MANUAL_NOTE = "수동 확인 필요: 트래픽이 이미 목적지로 넘어갔을 수 있습니다"

# 전환 단계 지표입니다. rolled_back·not_started 는 원본이 처리 중임이 확인된 결과이고,
# unknown·post_switch_failed·switched 는 트래픽이 넘어갔을 수 있는 결과입니다.
CUTOVER_OUTCOME = "cutover_outcome"
CUTOVER_ON_SOURCE = {"rolled_back", "not_started"}
_CLEANUP_STARTED: dict[str, Any] = {"cleanup_state": "running", "cleanup_message": "복제 자원 정리 중"}


class InvalidTransition(Exception):
    """지금 상태에서 할 수 없는 명령 (예: 승인 대기가 아닌데 승인)."""


def next_stage(run: RunStatus) -> StageName | None:
    return next((s for s in STAGE_ORDER if run.stage(s).state != WorkflowState.SUCCEEDED), None)


def _require_cutover_ready(run: RunStatus, acknowledge_unknown: bool) -> CutoverDecision:
    """승인 조건을 검사합니다. 통과하지 못하면 예외이고, 통과하면 그때의 판정을 돌려줍니다."""
    readiness = cutover_readiness(run)

    def titles(status: str) -> str:
        return ", ".join(c.title for c in readiness.checks if c.status == status)

    if readiness.decision == "no_go":
        raise InvalidTransition(f"전환 조건을 통과하지 못했습니다: {titles('fail')}")
    if readiness.decision == "needs_confirmation":
        if not acknowledge_unknown:
            raise InvalidTransition(f"확인되지 않은 항목이 있습니다: {titles('unknown')}. 확인 후 다시 승인하세요")
        logger.warning("run %s: 미확인 항목을 확인하고 전환 승인: %s", run.run_id, titles("unknown"))
    return readiness.decision


def _is_uncertain_cutover_gate(run: RunStatus, stage: StageName) -> bool:
    """결과를 모르는 전환 뒤 승인 ② 대기로 돌아온 상태입니다. 승인 조건이 막아도 기록할 길이 있어야 합니다."""
    if stage != StageName.CUTOVER or run.state != WorkflowState.WAITING_APPROVAL or run.waiting_gate != "cutover":
        return False
    outcome = run.stage(StageName.CUTOVER).metrics.get(CUTOVER_OUTCOME)
    return outcome is not None and outcome not in CUTOVER_ON_SOURCE


class Engine:
    def __init__(
        self,
        store: RunStore,
        runners: Mapping[StageName, StageRunner] | None = None,
        abort_cleanup: AbortCleanup | None = None,
        memory: ReasoningBank | None = None,
    ) -> None:
        self._store = store
        self._runners = dict(runners or {})
        self._abort_cleanup = abort_cleanup
        self._memory = memory  # 자가치유 기억(#77). 없으면 학습하지 않습니다

    # -- 조회 ---------------------------------------------------------------

    def get(self, run_id: str) -> RunStatus:
        return self._store.get(run_id)

    def list(self, limit: int = 50) -> list[RunStatus]:
        return self._store.list(limit)

    # -- 명령 ---------------------------------------------------------------

    def create_run(self, request: MigrationRequest) -> RunStatus:
        ts = now()
        run = RunStatus(
            run_id=f"mig-{ts:%Y%m%d}-{uuid4().hex[:8]}",
            state=WorkflowState.PENDING,
            current_stage=STAGE_ORDER[0],
            request=request,
            stages=[StageStatus(stage=s) for s in STAGE_ORDER],
            created_at=ts,
            updated_at=ts,
        )
        return self._store.create(run)

    def approve(
        self,
        run_id: str,
        gate: Gate,
        comment: str | None = None,
        approver: str | None = None,
        preflight_verdict: PreflightVerdict | None = None,
        preflight_measured_at: datetime | None = None,
        *,
        acknowledge_unknown: bool = False,
    ) -> RunStatus:
        # 점검 결과는 승인 ② 에만 있습니다. 다른 승인에 함께 와도 저장하지 않습니다.
        is_cutover = gate == "cutover"

        def change(run: RunStatus) -> RunStatus:
            if run.state != WorkflowState.WAITING_APPROVAL or run.waiting_gate != gate:
                raise InvalidTransition(f"{GATE_LABEL[gate]} 대기 상태가 아닙니다 (현재 {run.state.value})")
            decision = _require_cutover_ready(run, acknowledge_unknown) if is_cutover else None
            note = f": {comment}" if comment else ""
            ts = now()
            record = ApprovalRecord(
                gate=gate,
                approver=approver,
                comment=comment,
                preflight_verdict=preflight_verdict if is_cutover else None,
                preflight_measured_at=preflight_measured_at if is_cutover else None,
                readiness_decision=decision,
                at=ts,
            )
            return run.model_copy(
                update={
                    "state": WorkflowState.RUNNING,
                    "waiting_gate": None,
                    "approvals": [*run.approvals, gate],
                    "approval_records": [*run.approval_records, record],
                    "message": f"{GATE_LABEL[gate]} 완료{note}",
                    "updated_at": ts,
                }
            )

        return self._store.update(run_id, change)

    def abort(self, run_id: str, reason: str | None = None) -> RunStatus:
        def change(run: RunStatus) -> RunStatus:
            if run.state in _FINISHED:
                raise InvalidTransition(f"이미 끝난 run 입니다 ({run.state.value})")
            ts = now()
            stages = [
                s.model_copy(update={"state": WorkflowState.ABORTED, "updated_at": ts})
                if s.state == WorkflowState.RUNNING
                else s
                for s in run.stages
            ]
            note = f": {reason}" if reason else ""
            return run.model_copy(
                update={
                    "state": WorkflowState.ABORTED,
                    "waiting_gate": None,
                    "stages": stages,
                    "message": f"중단됨{note}",
                    "updated_at": ts,
                    **(_CLEANUP_STARTED if self._abort_cleanup else {}),
                }
            )

        return self._store.update(run_id, change)

    def retry_cleanup(self, run_id: str) -> RunStatus:
        """실패한 중단 정리를 다시 시도하도록 표시합니다. 이어서 run_abort_cleanup() 을 부릅니다."""

        def change(run: RunStatus) -> RunStatus:
            if run.state != WorkflowState.ABORTED or run.cleanup_state != "failed":
                raise InvalidTransition("다시 정리할 수 있는 상태가 아닙니다")
            return run.model_copy(update=_CLEANUP_STARTED)

        return self._store.update(run_id, change)

    def run_abort_cleanup(self, run_id: str) -> None:
        """중단된 run 이 남긴 외부 자원을 정리합니다. 백그라운드 작업으로 부릅니다. 실패는 기록하고 다시 시도하게 둡니다."""
        if self._abort_cleanup is None:
            return
        run = self._store.get(run_id)
        if run.state != WorkflowState.ABORTED or run.cleanup_state != "running":
            return
        try:
            has_cleaned = self._abort_cleanup(run)
        except Exception as exc:
            logger.exception("abort cleanup failed for run %s", run_id)
            from .runners import scrub_sensitive_text
            cleaned_err = scrub_sensitive_text(str(exc), fallback=type(exc).__name__)
            self._finish_cleanup(run_id, "failed", f"복제 자원 정리 실패: {cleaned_err}"[:_MAX_ERROR_MESSAGE])
            return
        if has_cleaned:
            self._finish_cleanup(run_id, "done", "복제 자원 정리 완료")
        else:
            self._finish_cleanup(run_id, None, None)

    def _finish_cleanup(self, run_id: str, state: CleanupState | None, message: str | None) -> None:
        # run.updated_at 은 상태가 바뀐 시각이므로 건드리지 않습니다.
        self._store.update(
            run_id,
            lambda run: run.model_copy(update={"cleanup_state": state, "cleanup_message": message}),
        )

    def submit_manual(self, run_id: str, stage: StageName, payload: ManualStageInput) -> RunStatus:
        """다음 차례 단계의 결과를 사람이 직접 넣습니다 (모듈 미연결·실패 시 fallback)."""

        def change(run: RunStatus) -> RunStatus:
            # 등록된 모듈이 돌 차례(pending·running)에 끼어들지 않도록 멈춘 run 에만 받습니다.
            is_uncertain = _is_uncertain_cutover_gate(run, stage)
            if run.state not in _MANUAL_ALLOWED and not is_uncertain:
                raise InvalidTransition(f"지금은 수동 입력을 받을 수 없습니다 (현재 {run.state.value})")
            if is_uncertain and not (payload.note or "").strip():
                raise InvalidTransition("결과를 모르는 전환을 기록하려면 확인한 내용을 메모로 남겨야 합니다")
            expected = next_stage(run)
            if stage != expected:
                raise InvalidTransition(f"지금 입력할 수 있는 단계는 {expected.value if expected else '없음'} 입니다")
            if run.stage(stage).state == WorkflowState.RUNNING:
                raise InvalidTransition(f"{stage.value} 단계가 실행 중입니다")
            gate = GATE_BEFORE.get(stage)
            if gate and gate not in run.approvals and not is_uncertain:
                raise InvalidTransition(f"{GATE_LABEL[gate]} 전에는 {stage.value} 결과를 넣을 수 없습니다")
            note = f": {payload.note}" if payload.note else ""
            updated = with_stage(
                run,
                stage,
                state=WorkflowState.SUCCEEDED,
                progress=100,
                manual=True,
                message=f"수동 입력{note}",
            )
            return updated.model_copy(
                update={
                    "state": WorkflowState.RUNNING,
                    "waiting_gate": None,
                    "outputs": {**run.outputs, stage.value: payload.output},
                    "message": f"{stage.value} 수동 입력",
                }
            )

        updated = self._store.update(run_id, change)
        self._learn(run_id, stage, "manual_input")
        return updated

    def resume(self, run_id: str) -> RunStatus:
        """실패한 단계를 다시 시도하거나, 진행이 끊긴 run 을 다시 진행시킵니다. 이어서 advance() 를 부릅니다."""

        def change(run: RunStatus) -> RunStatus:
            if run.state not in _RESUMABLE:
                raise InvalidTransition(f"지금은 재개할 수 없습니다 (현재 {run.state.value})")
            if any(s.state == WorkflowState.RUNNING for s in run.stages):
                raise InvalidTransition("실행 중인 단계가 있습니다")
            ts = now()
            stages = [
                s.model_copy(update={"state": WorkflowState.PENDING, "message": "재시도 대기", "updated_at": ts})
                if s.state == WorkflowState.FAILED
                else s
                for s in run.stages
            ]
            resumed = run.model_copy(
                update={"state": WorkflowState.RUNNING, "stages": stages, "message": "재개", "updated_at": ts}
            )
            cutover = run.stage(StageName.CUTOVER)
            if cutover.state != WorkflowState.FAILED:
                return resumed
            # 실패한 전환은 어떤 경우든 새 승인 ② 를 받습니다. 결과 기록이 없는 옛 row 는 어느 쪽인지 모르는 것으로 둡니다.
            approvals = [g for g in run.approvals if g != "cutover"]
            metrics = {CUTOVER_OUTCOME: "unknown", **cutover.metrics}
            return with_stage(resumed, StageName.CUTOVER, metrics=metrics).model_copy(update={"approvals": approvals})

        return self._store.update(run_id, change)

    # -- 진행 ---------------------------------------------------------------

    def advance(self, run_id: str) -> RunStatus:
        """멈출 지점(승인 대기·미구현·실패·완료)까지 단계를 순서대로 실행합니다. 백그라운드 작업으로 부릅니다."""
        try:
            return self._advance(run_id)
        except Exception:
            # 저장소 오류 등으로 run 이 running 에 멈추지 않도록 실패로 남깁니다 (가능한 경우).
            logger.exception("advance failed for run %s", run_id)
            self._mark_failed(run_id, None, "workflows 내부 오류 — 로그 확인 후 resume")
            raise

    def _advance(self, run_id: str) -> RunStatus:
        while True:
            claimed: list[StageName] = []

            def step(run: RunStatus) -> RunStatus:
                if run.state not in _ADVANCEABLE:
                    return run
                stage = next_stage(run)
                if stage is None:
                    return run.model_copy(
                        update={
                            "state": WorkflowState.SUCCEEDED,
                            "current_stage": None,
                            "message": "모든 단계 완료",
                            "updated_at": now(),
                        }
                    )
                gate = GATE_BEFORE.get(stage)
                if gate and gate not in run.approvals:
                    return run.model_copy(
                        update={
                            "state": WorkflowState.WAITING_APPROVAL,
                            "current_stage": stage,
                            "waiting_gate": gate,
                            "message": f"{GATE_LABEL[gate]} 대기",
                            "updated_at": now(),
                        }
                    )
                if run.stage(stage).state == WorkflowState.RUNNING:
                    return run  # 다른 작업이 이미 이 단계를 실행 중
                if stage not in self._runners:
                    updated = with_stage(run, stage, state=WorkflowState.PENDING, message="미구현 — 수동 입력 대기")
                    return updated.model_copy(
                        update={
                            "state": WorkflowState.PAUSED,
                            "current_stage": stage,
                            "message": f"{stage.value} 미구현 — 수동 입력 대기",
                        }
                    )
                claimed.append(stage)
                updated = with_stage(run, stage, state=WorkflowState.RUNNING, progress=0, message=None)
                return updated.model_copy(
                    update={"state": WorkflowState.RUNNING, "current_stage": stage, "message": None}
                )

            run = self._store.update(run_id, step)
            if not claimed:
                return run
            self._run_stage(run, claimed[0])

    def _run_stage(self, run: RunStatus, stage: StageName) -> None:
        ctx = StoreRunContext(self._store, run.run_id, stage)
        try:
            output = self._runners[stage](run, ctx)
            if isinstance(output, BaseModel):
                data = output.model_dump(mode="json")
            elif isinstance(output, Mapping):
                data = dict(output)
            else:  # dict() 는 ["ok", "hi"] 도 받아 주므로 가짜 성공을 막습니다
                raise TypeError(f"단계 출력은 BaseModel 또는 dict 여야 합니다: {type(output).__name__}")
        except RunAborted:
            return
        except Exception as exc:  # 모듈 오류는 run 을 멈추고 기록합니다
            logger.exception("stage %s failed in run %s", stage.value, run.run_id)
            self._mark_failed(run.run_id, stage, f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_MESSAGE])
            return

        def done(r: RunStatus) -> RunStatus:
            if r.state == WorkflowState.ABORTED:
                return r
            updated = with_stage(r, stage, state=WorkflowState.SUCCEEDED, progress=100)
            return updated.model_copy(update={"outputs": {**r.outputs, stage.value: data}})

        self._store.update(run.run_id, done)
        self._learn(run.run_id, stage, "retry")

    def _learn(self, run_id: str, stage: StageName, action: str) -> None:
        """진단까지 거친 장애의 단계가 실제로 SUCCEEDED 로 회복됐을 때만 기억에 올립니다. 기억 오류는 run 에 영향을 주지 않습니다."""
        if self._memory is None:
            return
        try:
            if self._store.get(run_id).stage(stage).state == WorkflowState.SUCCEEDED:
                self._memory.resolve(run_id, stage.value, action)
        except Exception:
            logger.warning("ReasoningBank 학습 실패 (run %s, %s)", run_id, stage.value, exc_info=True)

    def _learn_failure(self, run_id: str, stage: StageName) -> None:
        """기억을 따랐는데 같은 단계가 다시 실패하면 그 기억의 실패를 올립니다."""
        if self._memory is None:
            return
        try:
            self._memory.fail(run_id, stage.value)
        except Exception:
            logger.warning("ReasoningBank 실패 기록 실패 (run %s, %s)", run_id, stage.value, exc_info=True)

    def _mark_failed(self, run_id: str, stage: StageName | None, detail: str) -> None:
        """run(과 단계)을 failed 로 남깁니다. 이미 abort 됐으면 그대로 둡니다. 기록마저 실패하면 로그만 남깁니다.

        cutover 가 원본에 머문 채 실패하면 승인 ② 를 취소합니다. 다시 시도하려면 새 점검을 보고 다시 승인해야 합니다.
        트래픽이 넘어갔을 수 있으면 승인을 남겨, 사람이 확인한 뒤 수동 입력으로 완료를 기록할 수 있게 합니다.
        """

        def fail(r: RunStatus) -> RunStatus:
            if r.state in _FINISHED:
                return r
            failed = [stage] if stage else [s.stage for s in r.stages if s.state == WorkflowState.RUNNING]
            updated = r
            for name in failed:
                updated = with_stage(updated, name, state=WorkflowState.FAILED, message=detail)
            message = f"{stage.value} 실패" if stage else detail
            approvals = r.approvals
            if StageName.CUTOVER in failed:
                outcome = r.stage(StageName.CUTOVER).metrics.get(CUTOVER_OUTCOME)
                if outcome is None or outcome in CUTOVER_ON_SOURCE:
                    approvals = [g for g in r.approvals if g != "cutover"]
                    message = f"{message} — {_CUTOVER_RETRY_NOTE}"
                else:
                    message = f"{message} — {_CUTOVER_MANUAL_NOTE}"
            return updated.model_copy(
                update={"state": WorkflowState.FAILED, "approvals": approvals, "message": message, "updated_at": now()}
            )

        try:
            self._store.update(run_id, fail)
        except Exception:
            logger.exception("could not record failure for run %s", run_id)
            return
        if stage is not None:
            self._learn_failure(run_id, stage)
