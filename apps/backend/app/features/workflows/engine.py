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
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from .context import RunAborted, StoreRunContext, now, with_stage
from .schemas import (
    GATE_BEFORE,
    STAGE_ORDER,
    Gate,
    ManualStageInput,
    MigrationRequest,
    RunContext,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from .store import RunStore

logger = logging.getLogger(__name__)

StageRunner = Callable[[RunStatus, RunContext], BaseModel | dict[str, Any]]

GATE_LABEL: dict[Gate, str] = {"plan": "계획 승인 ①", "cutover": "전환 승인 ②"}

_ADVANCEABLE = {WorkflowState.PENDING, WorkflowState.RUNNING}
_FINISHED = {WorkflowState.SUCCEEDED, WorkflowState.ABORTED}
_MANUAL_ALLOWED = {WorkflowState.PAUSED, WorkflowState.FAILED}
_RESUMABLE = {WorkflowState.FAILED, WorkflowState.RUNNING, WorkflowState.PENDING}
_MAX_ERROR_MESSAGE = 500


class InvalidTransition(Exception):
    """지금 상태에서 할 수 없는 명령 (예: 승인 대기가 아닌데 승인)."""


def next_stage(run: RunStatus) -> StageName | None:
    return next((s for s in STAGE_ORDER if run.stage(s).state != WorkflowState.SUCCEEDED), None)


class Engine:
    def __init__(self, store: RunStore, runners: Mapping[StageName, StageRunner] | None = None) -> None:
        self._store = store
        self._runners = dict(runners or {})

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

    def approve(self, run_id: str, gate: Gate, comment: str | None = None) -> RunStatus:
        def change(run: RunStatus) -> RunStatus:
            if run.state != WorkflowState.WAITING_APPROVAL or run.waiting_gate != gate:
                raise InvalidTransition(f"{GATE_LABEL[gate]} 대기 상태가 아닙니다 (현재 {run.state.value})")
            note = f": {comment}" if comment else ""
            return run.model_copy(
                update={
                    "state": WorkflowState.RUNNING,
                    "waiting_gate": None,
                    "approvals": [*run.approvals, gate],
                    "message": f"{GATE_LABEL[gate]} 완료{note}",
                    "updated_at": now(),
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
                }
            )

        return self._store.update(run_id, change)

    def submit_manual(self, run_id: str, stage: StageName, payload: ManualStageInput) -> RunStatus:
        """다음 차례 단계의 결과를 사람이 직접 넣습니다 (모듈 미연결·실패 시 fallback)."""

        def change(run: RunStatus) -> RunStatus:
            # 등록된 모듈이 돌 차례(pending·running)에 끼어들지 않도록 멈춘 run 에만 받습니다.
            if run.state not in _MANUAL_ALLOWED:
                raise InvalidTransition(f"지금은 수동 입력을 받을 수 없습니다 (현재 {run.state.value})")
            expected = next_stage(run)
            if stage != expected:
                raise InvalidTransition(f"지금 입력할 수 있는 단계는 {expected.value if expected else '없음'} 입니다")
            if run.stage(stage).state == WorkflowState.RUNNING:
                raise InvalidTransition(f"{stage.value} 단계가 실행 중입니다")
            gate = GATE_BEFORE.get(stage)
            if gate and gate not in run.approvals:
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
                    "outputs": {**run.outputs, stage.value: payload.output},
                    "message": f"{stage.value} 수동 입력",
                }
            )

        return self._store.update(run_id, change)

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
            return run.model_copy(
                update={"state": WorkflowState.RUNNING, "stages": stages, "message": "재개", "updated_at": ts}
            )

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

    def _mark_failed(self, run_id: str, stage: StageName | None, detail: str) -> None:
        """run(과 단계)을 failed 로 남깁니다. 이미 abort 됐으면 그대로 둡니다. 기록마저 실패하면 로그만 남깁니다."""

        def fail(r: RunStatus) -> RunStatus:
            if r.state in _FINISHED:
                return r
            if stage is None:
                stuck = [s.stage for s in r.stages if s.state == WorkflowState.RUNNING]
                updated = r
                for name in stuck:
                    updated = with_stage(updated, name, state=WorkflowState.FAILED, message=detail)
                return updated.model_copy(update={"state": WorkflowState.FAILED, "message": detail, "updated_at": now()})
            updated = with_stage(r, stage, state=WorkflowState.FAILED, message=detail)
            return updated.model_copy(update={"state": WorkflowState.FAILED, "message": f"{stage.value} 실패"})

        try:
            self._store.update(run_id, fail)
        except Exception:
            logger.exception("could not record failure for run %s", run_id)
