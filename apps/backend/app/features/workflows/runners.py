"""단계별 모듈 연결. 각 runner 는 run 상태에서 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 부릅니다.

연결되지 않은 단계는 여기 없고, engine 이 `pending` + 수동 입력 대기로 둡니다.
모듈은 자기 `schemas.py` 와 진입 함수 `service.run` 만 씁니다 (확정서 2절).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .engine import StageRunner
from .schemas import RunContext, RunStatus, StageName

logger = logging.getLogger(__name__)


class MissingStageOutput(RuntimeError):
    """앞 단계 출력이 없어 이 단계 입력을 만들 수 없을 때."""


def run_planning(run: RunStatus, ctx: RunContext) -> Any:
    # 1. discovery 출력(수동 입력 fixture 또는 앞선 discovery 단계)이 있으면 planning 모듈 사용
    inventory = run.outputs.get(StageName.DISCOVERY.value)
    if inventory is not None:
        try:
            from app.features.planning import service as planning_service
            from app.features.planning.schemas import PlanningInput, TargetSelection

            inp = PlanningInput(inventory=inventory, target=TargetSelection(region=run.request.target_region))
            return planning_service.run(inp, ctx)
        except ImportError:
            pass

    # 2. assessment 모듈 지원 (PR #27): discovery+planning 통합 모듈
    try:
        from app.features.assessment import service as assessment_service
        from app.features.assessment.schemas import AssessmentInput

        aws_cred = (
            getattr(run.request, "aws_credentials_ref", None)
            or getattr(run.request, "aws_role_ref", None)
            or "secret://aws/default"
        )
        db_admin = (
            getattr(run.request, "db_admin_ref", None)
            or getattr(run.request, "db_credentials_ref", None)
        )
        inp = AssessmentInput(
            run_id=run.run_id,
            github_repo_url=run.request.github_repo_url,
            aws_credentials_ref=aws_cred,
            db_admin_ref=db_admin,
            aws_region=run.request.aws_region,
            target_region=run.request.target_region,
            ecs_service=run.request.ecs_service,
            rds_instance_id=run.request.rds_instance_id,
        )
        return assessment_service.run(inp, ctx)
    except ImportError:
        pass

    # 3. discovery 출력이 없고 assessment 모듈도 없는 경우
    if inventory is None:
        raise MissingStageOutput("discovery 출력이 없습니다. 원본 조사 결과를 먼저 넣어 주세요.")

    raise RuntimeError("planning 또는 assessment 모듈을 찾을 수 없습니다.")


RUNNERS: Mapping[StageName, StageRunner] = {
    StageName.PLANNING: run_planning,
}
