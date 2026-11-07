"""RunContext 구현체. 모듈의 report() 를 run 상태 테이블에 기록합니다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schemas import RunStatus, StageName, WorkflowState
from .store import RunStore


class RunAborted(BaseException):
    """실행 중인 단계가 report() 하는 시점에 run 이 이미 abort 된 경우.

    모듈의 `except Exception` 에 잡혀 abort 가 무시되지 않도록 BaseException 을 상속합니다.
    """


# 단계 종료(succeeded·failed·aborted)와 대기(pending)는 engine 이 run() 반환·예외로 정합니다.
# 모듈이 보고할 수 있는 건 진행 중 상태뿐이며, 저장되는 단계 state 는 계속 running 입니다.
_REPORTABLE = {WorkflowState.RUNNING, WorkflowState.WAITING_APPROVAL, WorkflowState.PAUSED}
# 끝난 단계는 보고로 다시 열지 않습니다. CDC 처럼 단계가 끝난 뒤에도 계속 보고하는 모듈의 최신 값만 받습니다.
_SETTLED_STAGE_STATES = {WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.ABORTED}


def now() -> datetime:
    return datetime.now(timezone.utc)


def with_stage(run: RunStatus, name: StageName, **changes: Any) -> RunStatus:
    """한 단계의 값만 바꾼 새 run 을 돌려줍니다 (원본은 그대로)."""
    ts = now()
    stages = [
        s.model_copy(update={**changes, "updated_at": ts}) if s.stage == name else s
        for s in run.stages
    ]
    return run.model_copy(update={"stages": stages, "updated_at": ts})


class StoreRunContext:
    def __init__(self, store: RunStore, run_id: str, stage: StageName) -> None:
        self._store = store
        self._run_id = run_id
        self._stage = stage

    def report(
        self,
        state: str,
        progress: int,
        metrics: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        parsed_state = WorkflowState(state)
        # 타 모듈(예: assessment/service.py)이 완료 시점에 report(state="succeeded", progress=100)을
        # 호출하는 경우가 있으므로, succeeded는 예외 대신 running(진행률 100%)으로 안전하게 수용합니다.
        if parsed_state == WorkflowState.SUCCEEDED:
            progress = 100
        elif parsed_state not in _REPORTABLE:
            raise ValueError(f"report() 로 보낼 수 없는 state 입니다: {state} (종료는 run() 반환·예외로)")

        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be 0..100, got {progress}")
        # with_stage() 는 검증을 안 하므로 소수(done/total*100)를 그대로 저장하면 run 을 다시 못 읽습니다.
        progress = int(progress)

        def change(run: RunStatus) -> RunStatus:
            if run.state == WorkflowState.ABORTED:
                raise RunAborted(self._run_id)
            current = run.stage(self._stage)
            merged = {**current.metrics, **(metrics or {})}
            text = message if message is not None else current.message
            if current.state in _SETTLED_STAGE_STATES:
                # 실패 메시지는 진단의 근거이므로 남깁니다. run.updated_at 은 상태가 바뀐 시각이므로 유지합니다.
                kept = current.message if current.state == WorkflowState.FAILED else text
                updated = with_stage(run, self._stage, metrics=merged, message=kept)
                return updated.model_copy(update={"updated_at": run.updated_at})
            return with_stage(
                run,
                self._stage,
                state=WorkflowState.RUNNING,
                progress=progress,
                metrics=merged,
                message=text,
            )

        self._store.update(self._run_id, change)
