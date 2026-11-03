"""단독 실행·확인용 API."""

from fastapi import APIRouter

from . import service
from .schemas import MigrationPlan, PlanningInput

router = APIRouter(prefix="/planning", tags=["planning"])


@router.post("/plan", response_model=MigrationPlan)
def create_plan(inp: PlanningInput) -> MigrationPlan:
    return service.run(inp)
