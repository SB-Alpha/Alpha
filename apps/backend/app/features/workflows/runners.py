"""단계별 모듈 연결. 각 runner 는 run 상태에서 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 부릅니다.

연결되지 않은 단계는 여기 없고, engine 이 `pending` + 수동 입력 대기로 둡니다.
모듈은 자기 `schemas.py` 와 진입 함수 `service.run` 만 씁니다 (확정서 2절).
"""

from __future__ import annotations

from collections.abc import Mapping

from app.features.planning import service as planning_service
from app.features.planning.schemas import MigrationPlan, PlanningInput, TargetSelection

from .engine import StageRunner
from .schemas import RunContext, RunStatus, StageName


class MissingStageOutput(RuntimeError):
    """앞 단계 출력이 없어 이 단계 입력을 만들 수 없을 때."""


def run_planning(run: RunStatus, ctx: RunContext) -> MigrationPlan:
    # discovery 출력(모듈 결과 또는 수동 입력 fixture)이 planning 의 inventory 입니다.
    inventory = run.outputs.get(StageName.DISCOVERY.value)
    if inventory is None:
        raise MissingStageOutput("discovery 출력이 없습니다. 원본 조사 결과를 먼저 넣어 주세요.")
    inp = PlanningInput(inventory=inventory, target=TargetSelection(region=run.request.target_region))
    return planning_service.run(inp, ctx)


RUNNERS: Mapping[StageName, StageRunner] = {
    StageName.PLANNING: run_planning,
}
