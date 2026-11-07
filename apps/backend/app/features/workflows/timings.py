"""단계별 실제 소요 시간. 저장된 run 기록에서 계산하며 예측하지 않습니다. (이주호)

- 한 단계의 시작 = 앞 단계가 끝난 시각. 승인 ①·② 앞의 단계는 승인한 시각 (사람이 기다린 시간은 따로 셉니다).
- 한 단계의 끝 = 그 단계가 마지막으로 바뀐 시각 (StageStatus.updated_at). 성공한 단계만 셉니다.
- 수동 입력으로 넘긴 단계, 시각이 없는 단계, 순서가 맞지 않는 단계는 값 없이 이유만 남깁니다 (지어내지 않음).
- baseline 은 지난 run 들의 실측 분포(최소·중앙값·최대)입니다. "예상"이 아니라 "과거 N회 실측" 으로 보여 주세요.
  모의(simulated) 실행이 섞인 run 은 빼고, 표본 수를 함께 돌려줍니다. 데이터 크기·클라우드 상태에 따라 달라집니다.
- 한계: 같은 단계를 다시 시도(retry)했으면 실패한 시도의 시간도 들어갑니다 (시도별 시각은 저장되지 않음).
"""

from __future__ import annotations

from statistics import median

from pydantic import BaseModel

from .schemas import GATE_BEFORE, STAGE_ORDER, ApprovalRecord, RunStatus, StageName, WorkflowState


class StageTiming(BaseModel):
    stage: StageName
    seconds: float | None = None  # 값이 없으면 note 에 이유
    note: str | None = None


class RunTimings(BaseModel):
    run_id: str
    stages: list[StageTiming]
    measured_total_seconds: float | None = None  # 잰 단계들의 합 (못 잰 단계는 빠짐)
    approval_wait_seconds: float | None = None  # 사람이 승인을 기다린 시간의 합


class StageBaseline(BaseModel):
    stage: StageName
    samples: int
    min_seconds: float | None = None
    median_seconds: float | None = None
    max_seconds: float | None = None


class TimingBaseline(BaseModel):
    runs_considered: int  # 분포에 들어간 run 수 (끝까지 성공한 실제 실행)
    stages: list[StageBaseline]
    note: str = ("과거에 실제로 끝까지 성공한 실행의 실측입니다. 데이터 크기와 클라우드 상태에 따라 달라지므로 "
                 "예상이 아니라 참고용입니다.")


def run_timings(run: RunStatus) -> RunTimings:
    out: list[StageTiming] = []
    prev_end = run.created_at
    measured = 0.0
    measured_any = False
    waited = 0.0
    waited_any = False

    for name in STAGE_ORDER:
        stage = run.stage(name)
        if stage.state != WorkflowState.SUCCEEDED:
            out.append(StageTiming(stage=name, note="아직 성공하지 않음"))
            continue
        if stage.updated_at is None:
            out.append(StageTiming(stage=name, note="완료 시각이 없음"))
            continue
        end = stage.updated_at

        start = prev_end
        gate = GATE_BEFORE.get(name)
        if gate:
            record = _last_approval(run, gate)
            if record is not None and record.at > start:
                waited += (record.at - start).total_seconds()
                waited_any = True
                start = record.at

        if stage.manual:
            out.append(StageTiming(stage=name, note="수동 입력으로 넘긴 단계라 재지 않음"))
        elif end < start:
            out.append(StageTiming(stage=name, note="기록된 시각의 순서가 맞지 않아 재지 않음"))
        else:
            seconds = round((end - start).total_seconds(), 1)
            out.append(StageTiming(stage=name, seconds=seconds))
            measured += seconds
            measured_any = True
        prev_end = max(prev_end, end)

    return RunTimings(
        run_id=run.run_id,
        stages=out,
        measured_total_seconds=round(measured, 1) if measured_any else None,
        approval_wait_seconds=round(waited, 1) if waited_any else None,
    )


def timing_baseline(runs: list[RunStatus]) -> TimingBaseline:
    """끝까지 성공한 실제 실행(모의·수동 입력이 섞이지 않은 run)만으로 단계별 분포를 만듭니다."""
    usable = [r for r in runs if _is_real_success(r)]
    per_stage: dict[StageName, list[float]] = {name: [] for name in STAGE_ORDER}
    for run in usable:
        for t in run_timings(run).stages:
            if t.seconds is not None:
                per_stage[t.stage].append(t.seconds)
    stages = [
        StageBaseline(
            stage=name,
            samples=len(values),
            min_seconds=min(values) if values else None,
            median_seconds=round(median(values), 1) if values else None,
            max_seconds=max(values) if values else None,
        )
        for name, values in per_stage.items()
    ]
    return TimingBaseline(runs_considered=len(usable), stages=stages)


def _is_real_success(run: RunStatus) -> bool:
    if run.state != WorkflowState.SUCCEEDED:
        return False
    if any(s.manual or s.metrics.get("simulated") for s in run.stages):
        return False
    return True


def _last_approval(run: RunStatus, gate: str) -> ApprovalRecord | None:
    records = [r for r in run.approval_records if r.gate == gate]
    return records[-1] if records else None
