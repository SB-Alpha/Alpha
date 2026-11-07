"""finops API. 가격 API 로 비용을 내는 /estimate 와, 이미 있는 비용을 비교만 하는 /report."""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from . import estimate, service
from .schemas import CostRequest, FinopsEstimate, FinopsInput, FinopsReport

# 서버가 뜰 때 큰 가격표를 백그라운드로 받아 둡니다 (첫 호출 20초 대기 방지, ALPHA_FINOPS_WARMUP=0 이면 끔)
router = APIRouter(prefix="/finops", tags=["finops"], on_startup=[estimate.warm_up])


@router.post("/estimate", response_model=FinopsEstimate)
async def make_estimate(req: CostRequest) -> FinopsEstimate:
    """크기를 받아 AWS·GCP·Azure 가격 API 단가로 월 비용을 내고, 절감 비교와 설명 문장을 붙입니다.

    단가를 못 받은 항목은 "미제공" 이고, 그런 후보는 절감액을 내지 않습니다. 가격 API 호출이 몇 초 걸려서
    스레드에서 돌립니다.
    """
    inp = await run_in_threadpool(estimate.build_input, req)
    return FinopsEstimate(input=inp, report=await run_in_threadpool(service.run, inp))


@router.post("/report", response_model=FinopsReport)
def make_report(inp: FinopsInput) -> FinopsReport:
    """비용 항목(수동 입력 포함)을 받아 절감 계산과 제안 문장만 돌려줍니다."""
    return service.run(inp)
