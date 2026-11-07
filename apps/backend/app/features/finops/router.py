"""단독 실행·확인용 API. 계약 확정 전이라 아직 main.py 에 등록하지 않았습니다."""

from fastapi import APIRouter

from . import service
from .schemas import FinopsInput, FinopsReport

router = APIRouter(prefix="/finops", tags=["finops"])


@router.post("/report", response_model=FinopsReport)
def make_report(inp: FinopsInput) -> FinopsReport:
    """비용 계산 결과를 받아 절감 계산과 제안 문장을 돌려줍니다."""
    return service.run(inp)
