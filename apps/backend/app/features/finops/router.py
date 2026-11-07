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


def arbitrage_request(req: CostRequest) -> ArbitrageRequest | None:
    """차익거래 입력. 하나라도 모르면 None 이고 분석을 내지 않습니다 (예전처럼 0.5 vCPU·150ms·읽기 80% 로 채우지 않음).

    차익거래는 원본 AWS(Fargate) 읽기를 GCP(Cloud Run) 로 돌리는 계산이라 AWS 원본 + GCP 후보일 때만 냅니다.
    """
    t = req.traffic
    gcp_region = next((c.region for c in req.candidates if c.cloud == "gcp"), None)
    if req.source_cloud != SOURCE_CLOUD or gcp_region is None or t is None:
        return None
    candidate = req.for_candidate()
    # 기존 차익거래 모델은 양쪽에 같은 크기를 씁니다. 다른 크기를 지원하기 전에는 잘못된 금액을 내지 않습니다.
    if (candidate.app_vcpu, candidate.app_memory_mib) != (req.app_vcpu, req.app_memory_mib):
        return None
    needed = (req.app_vcpu, req.app_memory_mib, t.avg_latency_ms, t.read_ratio, t.avg_response_kb)
    if any(v is None for v in needed):
        return None
    return ArbitrageRequest(
        source_cloud=SOURCE_CLOUD,
        target_cloud="gcp",
        source_region=req.source_region,
        target_region=gcp_region,
        app_vcpu=req.app_vcpu,
        app_memory_mib=req.app_memory_mib,
        app_instances=req.instances,
        rps=t.rps,
        read_ratio=t.read_ratio,
        idle_percent=t.idle_percent,
        avg_latency_ms=t.avg_latency_ms,
        avg_response_kb=t.avg_response_kb,
        replication_lag_bytes=t.replication_lag_bytes,  # 모르면 None 그대로 (0 으로 바꾸면 "지연 0, 안전"이 됩니다)
        max_safe_lag_bytes=t.max_safe_lag_bytes,
    )


@router.post("/estimate", response_model=FinopsEstimate)
async def make_estimate(req: CostRequest) -> FinopsEstimate:
    """크기를 받아 AWS·GCP·Azure 가격 API 단가로 월 비용을 내고, 절감 비교와 설명 문장을 붙입니다.

    단가를 못 받은 항목은 "미제공" 이고, 그런 후보는 절감액을 내지 않습니다. 가격 API 호출이 몇 초 걸려서
    스레드에서 돌립니다. 트래픽 조건이 있으면 Scale-to-Zero 차익거래 분석도 함께 산출합니다.
    """
    inp = await run_in_threadpool(estimate.build_input, req)
    rep = await run_in_threadpool(service.run, inp)

    arb_req = arbitrage_request(req)
    arb_result = await run_in_threadpool(_arbitrage, arb_req) if arb_req is not None else None

    return FinopsEstimate(input=inp, report=rep, arbitrage=arb_result)


@router.post("/arbitrage", response_model=ArbitrageResult)
async def calculate_arbitrage_route(req: ArbitrageRequest) -> ArbitrageResult:
    """비용 변동성과 트래픽 패턴, 복제 지연을 종합하여 동적 차익거래 최적 라우팅을 계산합니다."""
    return await run_in_threadpool(_arbitrage, req)


@router.post("/report", response_model=FinopsReport)
def make_report(inp: FinopsInput) -> FinopsReport:
    """비용 항목(수동 입력 포함)을 받아 절감 계산과 제안 문장만 돌려줍니다."""
    return service.run(inp)
