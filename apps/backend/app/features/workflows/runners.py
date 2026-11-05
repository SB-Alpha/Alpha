"""단계별 모듈 연결. 각 runner 는 run 상태에서 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 부릅니다.

연결되지 않은 단계는 여기 없고, engine 이 `pending` + 수동 입력 대기로 둡니다.
모듈은 자기 `schemas.py` 와 진입 함수 `service.run` 만 씁니다 (확정서 2절).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .engine import StageRunner
from .schemas import RunContext, RunStatus, StageName


class MissingStageOutput(RuntimeError):
    """요청이나 앞 단계 출력이 없어 이 단계 입력을 만들 수 없을 때."""


DISCOVERY_MERGED_MESSAGE = "assessment 에 통합 — 원본 조회는 planning 단계에서 합니다"


def run_discovery(run: RunStatus, ctx: RunContext) -> dict[str, Any]:
    # 11/4 합의로 discovery·planning 은 assessment 하나로 합쳤습니다 (PR #27). 원본 조회 결과는
    # planning 출력의 `source` 에 있으므로 이 단계는 조회 없이 그 사실만 남기고 넘깁니다.
    ctx.report("running", 100, None, DISCOVERY_MERGED_MESSAGE)
    return {"merged_into": StageName.PLANNING.value, "note": DISCOVERY_MERGED_MESSAGE}


def run_assessment(run: RunStatus, ctx: RunContext) -> Any:
    # assessment 가 discovery·planning 을 합친 모듈입니다 (PR #27). 출력은 {source, plan, gaps}.
    req = run.request
    if not req.aws_credentials_ref:
        raise MissingStageOutput("원본 AWS 키 이름표가 없습니다")

    try:
        from app.features.assessment import service as assessment_service
        from app.features.assessment.schemas import AssessmentInput
    except ImportError as exc:
        raise RuntimeError("assessment 모듈을 찾을 수 없습니다") from exc

    inp = AssessmentInput(
        run_id=run.run_id,
        github_repo_url=req.github_repo_url,
        aws_credentials_ref=req.aws_credentials_ref,
        db_admin_ref=req.db_admin_ref or req.db_credentials_ref,
        aws_region=req.aws_region,
        target_region=req.target_region,
        ecs_service=req.ecs_service,
        rds_instance_id=req.rds_instance_id,
    )
    return assessment_service.run(inp, ctx)


RUNNERS: Mapping[StageName, StageRunner] = {
    StageName.DISCOVERY: run_discovery,
    StageName.PLANNING: run_assessment,
}
