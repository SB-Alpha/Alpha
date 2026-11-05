"""단독 실행·확인용 API."""

from fastapi import APIRouter

from . import plan, service
from .schemas import AssessmentInput, AssessmentOutput, MigrationPlan, PlanningInput

router = APIRouter(prefix="/assessment", tags=["assessment"])


@router.post("/run", response_model=AssessmentOutput)
def run_assessment(inp: AssessmentInput) -> AssessmentOutput:
    """원본을 실제로 조회해서 판정합니다."""
    return service.run(inp)


@router.post("/plan", response_model=MigrationPlan)
def plan_from_inventory(inp: PlanningInput) -> MigrationPlan:
    """조회 없이 원본 사실을 직접 넣어 계획만 만듭니다 (수동 fixture 확인용)."""
    return plan.build(inp)
