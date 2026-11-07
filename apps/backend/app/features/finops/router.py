"""finops API. 가격 API 로 비용을 내는 /estimate 와, 이미 있는 비용을 비교만 하는 /report."""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from . import arbitrage, estimate, service
from .schemas import (
    SOURCE_CLOUD,
    ArbitrageRequest,
    ArbitrageResult,
    CostRequest,
    FinopsEstimate,
    FinopsInput,
    FinopsReport,
)

# 서버가 뜰 때 큰 가격표를 백그라운드로 받아 둡니다 (첫 호출 20초 대기 방지, ALPHA_FINOPS_WARMUP=0 이면 끔)
router = APIRouter(prefix="/finops", tags=["finops"], on_startup=[estimate.warm_up])


def _price_sources() -> tuple[estimate.AwsPrices, estimate.GcpPrices]:
    """차익거래 egress 단가용 가격 객체. build_input 의 기본값과 같은 가격 API(가격표는 프로세스에 6시간 보관)."""
    return estimate.AwsPrices(), estimate.GcpPrices()


def _arbitrage(req: ArbitrageRequest) -> ArbitrageResult:
    aws, gcp = _price_sources()
    return arbitrage.calculate_arbitrage(req, aws=aws, gcp=gcp)


@router.post("/estimate", response_model=FinopsEstimate)
async def make_estimate(req: CostRequest) -> FinopsEstimate:
    """크기를 받아 AWS·GCP·Azure 가격 API 단가로 월 비용을 내고, 절감 비교와 설명 문장을 붙입니다.

    단가를 못 받은 항목은 "미제공" 이고, 그런 후보는 절감액을 내지 않습니다. 가격 API 호출이 몇 초 걸려서
    스레드에서 돌립니다. 트래픽 조건이 있으면 Scale-to-Zero 차익거래 분석도 함께 산출합니다.
    """
    inp = await run_in_threadpool(estimate.build_input, req)
    rep = await run_in_threadpool(service.run, inp)

    # 읽기 분산 대상은 GCP 후보입니다. GCP 후보가 없으면 리전을 짐작하지 않고 분석을 내지 않습니다
    gcp_region = next((c.region for c in req.candidates if c.cloud == "gcp"), None)
    arb_result = None
    if req.traffic is not None and gcp_region is not None:
        arb_req = ArbitrageRequest(
            source_cloud=SOURCE_CLOUD,
            target_cloud="gcp",
            source_region=req.source_region,
            target_region=gcp_region,
            app_vcpu=req.app_vcpu or 0.5,
            app_memory_mib=req.app_memory_mib or 1024,
            app_instances=req.app_instances,
            rps=req.traffic.rps,
            read_ratio=0.8,
            idle_percent=req.traffic.idle_percent,
            avg_latency_ms=req.traffic.avg_latency_ms or 150.0,
            replication_lag_bytes=0 if req.traffic.replication_lag_bytes is None else req.traffic.replication_lag_bytes,
            max_safe_lag_bytes=req.traffic.max_safe_lag_bytes,
        )
        arb_result = await run_in_threadpool(_arbitrage, arb_req)

    return FinopsEstimate(input=inp, report=rep, arbitrage=arb_result)


@router.post("/arbitrage", response_model=ArbitrageResult)
async def calculate_arbitrage_route(req: ArbitrageRequest) -> ArbitrageResult:
    """비용 변동성과 트래픽 패턴, 복제 지연을 종합하여 동적 차익거래 최적 라우팅을 계산합니다."""
    return await run_in_threadpool(_arbitrage, req)


@router.post("/report", response_model=FinopsReport)
def make_report(inp: FinopsInput) -> FinopsReport:
    """비용 항목(수동 입력 포함)을 받아 절감 계산과 제안 문장만 돌려줍니다."""
    return service.run(inp)
