"""비용 변동성 기반 동적 라우팅 및 인프라 차익거래 (Dynamic Cost-Arbitrage Routing).

소프트뱅크의 문제의식 대응:
1. 환율 변동성(FX Exposure) 반영 (USD, JPY, KRW). 환율은 표에 적어 두지 않고 요청의 custom_fx_rate 만 씁니다.
   USD 가 아닌데 환율이 없으면 금액은 None("미제공") 입니다.
2. 읽기/쓰기 분리 차익거래 (Read-Write Split Arbitrage):
   - 쓰기 트래픽: Active Master 클라우드로 100% 펜싱(Fencing)하여 분산 DB 정합성 보장
   - 읽기 트래픽: CDC 동기화된 타깃 복제본으로 동적 가중치 분산
3. 세이프가드:
   - 복제 지연(Replication Lag)이 안전 기준을 초과하면 읽기 트래픽도 마스터로 즉시 100% 강제 회귀
   - 인터넷 송신(Egress) 단가는 가격 API(AWS Price List · GCP Cloud Billing Catalog)에서 받습니다.
     받지 못하면 읽기 분산 판단과 절감액을 내지 않습니다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

from .estimate import (
    HOURS_PER_MONTH,
    SECONDS_PER_MONTH,
    AwsPrices,
    GcpPrices,
)
from .schemas import (
    ArbitrageRequest,
    ArbitrageResult,
)

log = logging.getLogger(__name__)

# Cloud Run 표준 단가 (Seoul asia-northeast3 기준 기본 백업 단가)
CLOUD_RUN_VCPU_SEC_DEFAULT = 0.00002400
CLOUD_RUN_MEM_GIB_SEC_DEFAULT = 0.00000250
CLOUD_RUN_REQ_PER_MILLION_DEFAULT = 0.40

# Fargate 표준 단가 (Seoul ap-northeast-2 기준 기본 백업 단가)
FARGATE_VCPU_HOUR_DEFAULT = 0.04048
FARGATE_GB_HOUR_DEFAULT = 0.004445

NO_EGRESS = "미제공: egress 단가를 가격 API에서 받지 못해 읽기 분산 판단을 하지 않습니다."
GCP_CATALOG_NAME = "gcp-cloud-billing-catalog"


@dataclass(frozen=True)
class _Egress:
    net_per_gb: float | None  # 타깃 송신 단가 - 원본 송신 단가 (USD/GB). 모르면 None
    source: str | None
    notes: list[str]


@dataclass(frozen=True)
class _Decision:
    weight_target: float | None
    weight_source: float | None
    savings_usd: float | None
    savings_pct: float | None
    arbitrage_cost_usd: float | None
    recommendation: str
    notes: list[str]


def _egress(req: ArbitrageRequest, aws: AwsPrices | None, gcp: GcpPrices | None, stamp: str) -> _Egress:
    """읽기 응답을 원본 대신 타깃이 내보낼 때의 순 송신 단가. 가격 객체가 없으면 네트워크를 부르지 않습니다."""
    if aws is None or gcp is None:
        return _Egress(None, None, ["egress 단가: 가격 API 를 쓰지 않는 호출이라 조회하지 않았습니다."])
    try:
        out = aws.internet_egress(req.source_region)
        into = gcp.internet_egress(req.target_region)
    except Exception as exc:  # noqa: BLE001 - 단가가 없으면 None 입니다. 고정 단가로 채우지 않습니다
        log.warning("finops egress 단가 조회 실패 (%s): %s", type(exc).__name__, exc)
        return _Egress(None, None, [f"egress 단가: 가격 API 조회에 실패했습니다 ({type(exc).__name__})."])
    net = into.usd - out.usd
    source = f"{aws.api_name} {stamp} {out.sku} · {GCP_CATALOG_NAME} {stamp} {into.sku}"
    return _Egress(net, source, [
        (f"egress 가정: 읽기 응답을 원본(AWS) 대신 타깃(GCP)이 사용자에게 내보내므로 인터넷 송신 단가 차이만 셉니다. "
         f"GCP {into.usd:g} - AWS {out.usd:g} = {net:+.4f} USD/GB (음수면 송신 요금이 줄어듦)."),
        ("egress 단가는 구간 요금 중 첫 유료 구간입니다. 사용자는 타깃 리전과 같은 대륙에 있다고 보고, "
         "무료 할당량은 빼지 않으며, AWS(GB)·GCP(GiB) 단가를 같은 단위로 봤습니다."),
    ])


def _decide(req: ArbitrageRequest, lag_fenced: bool, read_rps: float, baseline_usd: float,
            target_app_usd: float, egress_usd: float | None, lag_unknown: bool = False) -> _Decision:
    """IDLE·복제 지연 펜싱은 egress 와 상관없이 정해지고, 그 밖의 판단은 egress 를 알 때만 합니다."""
    keep = (0.0, 1.0, 0.0, 0.0, baseline_usd)
    if read_rps <= 0:
        return _Decision(*keep, "IDLE: 활성 요청(RPS=0)이 없어 차익거래 대상이 없습니다.",
                         ["유휴 상태(RPS 0)에서는 상시 인스턴스 기본 비용이 유지되며 차익거래 우회가 불필요합니다."])
    if lag_unknown:
        return _Decision(*keep, "미제공: 복제 지연을 받지 못해 읽기 우회를 하지 않고 원본 마스터에 고정했습니다.", [])
    if lag_fenced:
        return _Decision(*keep, "FENCED_PRIMARY: 복제 지연으로 인해 모든 트래픽이 원본 마스터에 고정되었습니다.", [])
    if egress_usd is None:
        return _Decision(None, None, None, None, None, NO_EGRESS, [])

    arbitrage_total_usd = target_app_usd + egress_usd  # 읽기를 타깃으로 보낼 때: 타깃 앱 요금 + 송신 요금 차이
    read_portion_usd = baseline_usd * req.read_ratio  # Fargate 읽기 담당 추정 몫
    if arbitrage_total_usd >= read_portion_usd:
        return _Decision(*keep, "FENCED_PRIMARY: 트래픽 밀도 및 Egress 비용으로 인해 현재는 원본 Fargate 유지가 더 경제적입니다.",
                         ["고밀도 트래픽 또는 Egress 요금으로 인해 Scale-to-Zero 전환 이점이 상쇄되었습니다."])
    savings = read_portion_usd - arbitrage_total_usd
    pct = round(savings / baseline_usd * 100.0, 1) if baseline_usd > 0 else 0.0
    return _Decision(
        1.0, 0.0, savings, pct, baseline_usd - read_portion_usd + arbitrage_total_usd,
        (f"DYNAMIC_ARBITRAGE: 읽기 트래픽({req.read_ratio * 100:g}%)을 Scale-to-Zero 타깃(Cloud Run)으로 "
         f"우회하여 Egress 비용을 감안하고도 월 ${savings:,.2f} 절감됩니다."),
        [(f"Scale-to-Zero 유휴 절감: 유휴 {req.idle_percent:g}%, 평균 {read_rps:g} Read RPS에서 "
          f"요청 처리 시간만 과금되어 상시 인스턴스 대비 비용 우위를 달성했습니다.")],
    )


def _crossover_rps(req: ArbitrageRequest, baseline_usd: float, active_seconds: float,
                   net_per_gb: float | None) -> float | None:
    """입력 RPS 와 무관한 해석적 폐쇄형 계산: Cloud Run 비용 == Fargate 비용 지점. egress 를 모르면 None."""
    if net_per_gb is None or active_seconds <= 0 or req.avg_latency_ms <= 0 or req.read_ratio <= 0:
        return None
    gib = req.app_memory_mib / 1024.0
    compute_cost_per_sec = req.app_vcpu * CLOUD_RUN_VCPU_SEC_DEFAULT + gib * CLOUD_RUN_MEM_GIB_SEC_DEFAULT
    per_req = ((req.avg_latency_ms / 1000.0) * compute_cost_per_sec
               + CLOUD_RUN_REQ_PER_MILLION_DEFAULT / 1_000_000.0
               + (req.avg_response_kb / (1024.0 * 1024.0)) * net_per_gb)
    if per_req <= 0:
        return None
    return round(baseline_usd / (active_seconds * per_req), 1)


def _fx(req: ArbitrageRequest) -> tuple[float | None, list[str]]:
    """USD 는 1.0. JPY·KRW 는 custom_fx_rate 만 씁니다. 없으면 None 이고 금액을 환산하지 않습니다."""
    if req.currency == "USD":
        return 1.0, (["USD 요청이라 custom_fx_rate 는 쓰지 않았습니다."] if req.custom_fx_rate is not None else [])
    if req.custom_fx_rate is None:
        return None, [f"{req.currency} 환율(custom_fx_rate)이 미제공이라 금액을 환산하지 않았습니다. 고정 환율로 채우지 않습니다."]
    return req.custom_fx_rate, []


def _convert(amount_usd: float | None, currency: str, rate: float | None) -> float | None:
    if amount_usd is None or rate is None:
        return None
    return round(amount_usd * rate, 2 if currency == "USD" else 0)


def calculate_arbitrage(
    req: ArbitrageRequest,
    aws: AwsPrices | None = None,
    gcp: GcpPrices | None = None,
    today: date | None = None,
) -> ArbitrageResult:
    """비용 변동성과 트래픽 패턴, 복제 지연을 종합하여 동적 차익거래 최적 라우팅을 계산합니다.

    aws·gcp 가격 객체가 없으면 egress 단가를 조회하지 않고 None 으로 둡니다 (네트워크 호출 없음).
    """
    stamp = (today or date.today()).isoformat()
    reasons: list[str] = []
    gib = req.app_memory_mib / 1024.0

    # 1. 원본 상시 가동 월 비용 (Baseline Fargate, 내내 켜짐)
    baseline_usd = req.app_instances * (
        req.app_vcpu * FARGATE_VCPU_HOUR_DEFAULT + gib * FARGATE_GB_HOUR_DEFAULT
    ) * HOURS_PER_MONTH

    # 2. 복제 지연(Replication Lag) 세이프가드 검사
    # 지연을 모르면(None) 안전하다고 보지 않고 읽기 우회를 하지 않습니다. 모르는 값을 0 으로 채우지 않는 원칙과 같습니다.
    lag_unknown = req.replication_lag_bytes is None
    lag_fenced = lag_unknown or req.replication_lag_bytes > req.max_safe_lag_bytes
    if lag_unknown:
        reasons.append("미제공: 복제 지연을 받지 못해 읽기 우회를 하지 않고 원본 마스터로 100% 펜싱했습니다.")
    elif lag_fenced:
        reasons.append(
            f"복제 지연({req.replication_lag_bytes:,} B)이 안전 임계치({req.max_safe_lag_bytes:,} B)를 "
            f"초과하여 데이터 일관성 보호를 위해 읽기 우회를 차단하고 원본 마스터로 100% 펜싱했습니다."
        )

    # 3. 읽기 트래픽 및 Scale-to-Zero 비용 연산 (Poisson 도착 및 동시성 모델링)
    read_rps = req.rps * req.read_ratio
    active_seconds = SECONDS_PER_MONTH * (1.0 - req.idle_percent / 100.0)
    total_read_requests = read_rps * active_seconds
    busy_ratio = 0.0
    if read_rps > 0 and req.avg_latency_ms > 0:
        busy_ratio = min(1.0, 1.0 - math.exp(-read_rps * (req.avg_latency_ms / 1000.0)))
    target_app_usd = (active_seconds * busy_ratio
                      * (req.app_vcpu * CLOUD_RUN_VCPU_SEC_DEFAULT + gib * CLOUD_RUN_MEM_GIB_SEC_DEFAULT)
                      + total_read_requests / 1_000_000.0 * CLOUD_RUN_REQ_PER_MILLION_DEFAULT)

    # 4. 읽기 응답 송신 요금 차이 (가격 API 단가)
    egress = _egress(req, aws, gcp, stamp)
    egress_gb = (total_read_requests * req.avg_response_kb) / (1024.0 * 1024.0)
    egress_usd = None if egress.net_per_gb is None else egress_gb * egress.net_per_gb

    # 5. 차익거래 분기 판정
    decision = _decide(req, lag_fenced, read_rps, baseline_usd, target_app_usd, egress_usd, lag_unknown)
    reasons += decision.notes + egress.notes
    reasons.append("쓰기 트래픽(Write)은 데이터 정합성 보장을 위해 Active Master로 100% 펜싱됩니다.")
    reasons.append(
        "세션 일관성 한계: 쓰기 후 읽기 세션 고정(Read-After-Write Sticky) 미구현 상태이므로, "
        "방금 쓴 데이터가 복제 지연 시간 동안 일시적으로 이전 값으로 조회될 수 있습니다."
    )
    fx_rate, fx_notes = _fx(req)
    reasons += fx_notes

    return ArbitrageResult(
        read_weight_target=decision.weight_target,
        read_weight_source=decision.weight_source,
        write_destination="SOURCE",
        active_master="SOURCE",
        lag_fenced=lag_fenced,
        baseline_monthly_cost=_convert(baseline_usd, req.currency, fx_rate),
        arbitrage_monthly_cost=_convert(decision.arbitrage_cost_usd, req.currency, fx_rate),
        monthly_savings=_convert(decision.savings_usd, req.currency, fx_rate),
        savings_percent=decision.savings_pct,
        cross_cloud_egress_monthly_usd=None if egress_usd is None else round(egress_usd, 2),
        egress_usd_per_gb=None if egress.net_per_gb is None else round(egress.net_per_gb, 6),
        egress_source=egress.source,
        currency=req.currency,
        fx_rate=fx_rate,
        crossover_rps=_crossover_rps(req, baseline_usd, active_seconds, egress.net_per_gb),
        recommendation=decision.recommendation,
        reasons=reasons,
    )
