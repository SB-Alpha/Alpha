"""비용 변동성 기반 동적 라우팅 및 인프라 차익거래 (Dynamic Cost-Arbitrage Routing).

소프트뱅크의 문제의식 대응:
1. 환율 변동성(FX Exposure) 반영 (USD, JPY, KRW)
2. 읽기/쓰기 분리 차익거래 (Read-Write Split Arbitrage):
   - 쓰기 트래픽: Active Master 클라우드로 100% 펜싱(Fencing)하여 분산 DB 정합성 보장
   - 읽기 트래픽: CDC 동기화된 타깃 복제본으로 동적 가중치 분산
3. 세이프가드:
   - 복제 지연(Replication Lag)이 안전 기준을 초과하면 읽기 트래픽도 마스터로 즉시 100% 강제 회귀
   - 크로스 클라우드 네트워크 Egress 비용($0.09/GB) 산정 반영
"""

from __future__ import annotations

import math
from typing import Any

from .estimate import (
    HOURS_PER_MONTH,
    SECONDS_PER_MONTH,
    AwsPrices,
    GcpPrices,
    CostRequest,
    Candidate,
    Traffic,
)
from .schemas import (
    DEFAULT_FX_RATES,
    ArbitrageRequest,
    ArbitrageResult,
)

# 클라우드 간 인터넷 / 리전 간 Egress 단가 (AWS/GCP 표준 기준)
EGRESS_COST_PER_GB_USD = 0.09

# Cloud Run 표준 단가 (Seoul asia-northeast3 기준 기본 백업 단가)
CLOUD_RUN_VCPU_SEC_DEFAULT = 0.00002400
CLOUD_RUN_MEM_GIB_SEC_DEFAULT = 0.00000250
CLOUD_RUN_REQ_PER_MILLION_DEFAULT = 0.40

# Fargate 표준 단가 (Seoul ap-northeast-2 기준 기본 백업 단가)
FARGATE_VCPU_HOUR_DEFAULT = 0.04048
FARGATE_GB_HOUR_DEFAULT = 0.004445


def _calc_fx(amount_usd: float, currency: str, custom_rate: float | None) -> float:
    rate = custom_rate or DEFAULT_FX_RATES.get(currency.upper(), 1.0)
    return round(amount_usd * rate, 2 if currency == "USD" else 0)


def calculate_arbitrage(
    req: ArbitrageRequest,
    aws: AwsPrices | None = None,
    gcp: GcpPrices | None = None,
) -> ArbitrageResult:
    """비용 변동성과 트래픽 패턴, 복제 지연을 종합하여 동적 차익거래 최적 라우팅을 계산합니다."""
    fx_rate = req.custom_fx_rate or DEFAULT_FX_RATES.get(req.currency.upper(), 1.0)
    reasons: list[str] = []

    # 1. 원본 상시 가동 월 비용 (Baseline Fargate)
    active_share = 1.0 - (req.idle_percent / 100.0)
    gib = req.app_memory_mib / 1024.0

    # Fargate 인스턴스 비용 (내내 켜짐)
    baseline_fargate_app_usd = req.app_instances * (
        req.app_vcpu * FARGATE_VCPU_HOUR_DEFAULT + gib * FARGATE_GB_HOUR_DEFAULT
    ) * HOURS_PER_MONTH

    # 2. 복제 지연(Replication Lag) 세이프가드 검사
    lag_fenced = False
    if req.replication_lag_bytes is not None and req.replication_lag_bytes > req.max_safe_lag_bytes:
        lag_fenced = True
        reasons.append(
            f"복제 지연({req.replication_lag_bytes:,} B)이 안전 임계치({req.max_safe_lag_bytes:,} B)를 "
            f"초과하여 데이터 일관성 보호를 위해 읽기 우회를 차단하고 원본 마스터로 100% 펜싱했습니다."
        )

    # 3. 읽기 트래픽 및 Scale-to-Zero 비용 연산
    read_rps = req.rps * req.read_ratio
    active_seconds = SECONDS_PER_MONTH * active_share
    total_read_requests = read_rps * active_seconds

    # Cloud Run 동시성 및 요청 활성 시간 (Poisson 도착 및 동시성 모델링)
    busy_ratio = 0.0
    if read_rps > 0 and req.avg_latency_ms > 0:
        busy_ratio = min(1.0, 1.0 - math.exp(-read_rps * (req.avg_latency_ms / 1000.0)))

    target_busy_seconds = active_seconds * busy_ratio
    target_compute_cost_usd = target_busy_seconds * (
        req.app_vcpu * CLOUD_RUN_VCPU_SEC_DEFAULT + gib * CLOUD_RUN_MEM_GIB_SEC_DEFAULT
    )
    target_request_cost_usd = (total_read_requests / 1_000_000.0) * CLOUD_RUN_REQ_PER_MILLION_DEFAULT
    target_app_cost_usd = target_compute_cost_usd + target_request_cost_usd

    # 크로스 클라우드 응답 Egress 비용
    egress_gb = (total_read_requests * req.avg_response_kb) / (1024.0 * 1024.0)
    cross_cloud_egress_usd = egress_gb * EGRESS_COST_PER_GB_USD

    # 4. 차익거래 분기 판정
    # 읽기를 타깃으로 보낼 때의 총비용 (타깃 앱 요금 + Egress 요금)
    arbitrage_total_app_usd = target_app_cost_usd + cross_cloud_egress_usd

    # Fargate 읽기 담당 추정 몫
    fargate_read_portion_usd = baseline_fargate_app_usd * req.read_ratio

    if read_rps <= 0:
        read_weight_target = 0.0
        read_weight_source = 1.0
        monthly_savings_usd = 0.0
        savings_pct = 0.0
        arbitrage_monthly_cost_usd = baseline_fargate_app_usd
        recommendation = "IDLE: 활성 요청(RPS=0)이 없어 차익거래 대상이 없습니다."
        reasons.append("유휴 상태(RPS 0)에서는 상시 인스턴스 기본 비용이 유지되며 차익거래 우회가 불필요합니다.")
    elif lag_fenced:
        read_weight_target = 0.0
        read_weight_source = 1.0
        monthly_savings_usd = 0.0
        savings_pct = 0.0
        arbitrage_monthly_cost_usd = baseline_fargate_app_usd
        recommendation = "FENCED_PRIMARY: 복제 지연으로 인해 모든 트래픽이 원본 마스터에 고정되었습니다."
    elif arbitrage_total_app_usd < fargate_read_portion_usd:
        # 차익거래가 유리함!
        read_weight_target = 1.0
        read_weight_source = 0.0
        # 절감액 = Fargate 읽기 몫 - (Cloud Run 비용 + Egress 비용)
        monthly_savings_usd = max(0.0, fargate_read_portion_usd - arbitrage_total_app_usd)
        arbitrage_monthly_cost_usd = (baseline_fargate_app_usd - fargate_read_portion_usd) + arbitrage_total_app_usd
        savings_pct = round((monthly_savings_usd / baseline_fargate_app_usd) * 100.0, 1) if baseline_fargate_app_usd > 0 else 0.0
        recommendation = (
            f"DYNAMIC_ARBITRAGE: 읽기 트래픽({req.read_ratio * 100:g}%)을 Scale-to-Zero 타깃(Cloud Run)으로 "
            f"우회하여 Egress 비용을 감안하고도 월 ${monthly_savings_usd:,.2f} 절감됩니다."
        )
        reasons.append(
            f"Scale-to-Zero 유휴 절감: 유휴 {req.idle_percent:g}%, 평균 {read_rps:g} Read RPS에서 "
            f"요청 처리 시간만 과금되어 상시 인스턴스 대비 비용 우위를 달성했습니다."
        )
    else:
        # Fargate 유지가 더 저렴 (RPS가 너무 높거나 Egress 비용이 절감액을 초과)
        read_weight_target = 0.0
        read_weight_source = 1.0
        monthly_savings_usd = 0.0
        savings_pct = 0.0
        arbitrage_monthly_cost_usd = baseline_fargate_app_usd
        recommendation = "FENCED_PRIMARY: 트래픽 밀도 및 Egress 비용으로 인해 현재는 원본 Fargate 유지가 더 경제적입니다."
        reasons.append("고밀도 트래픽 또는 Egress 요금으로 인해 Scale-to-Zero 전환 이점이 상쇄되었습니다.")

    reasons.append("쓰기 트래픽(Write)은 데이터 정합성 보장을 위해 Active Master로 100% 펜싱됩니다.")
    reasons.append(
        "세션 일관성 한계: 쓰기 후 읽기 세션 고정(Read-After-Write Sticky) 미구현 상태이므로, "
        "방금 쓴 데이터가 복제 지연 시간 동안 일시적으로 이전 값으로 조회될 수 있습니다."
    )

    # 변곡점 RPS 분석 (입력 RPS와 무관한 해석적 폐쇄형 계산: Cloud Run 비용 == Fargate 비용 지점)
    crossover_rps = None
    if active_seconds > 0 and req.avg_latency_ms > 0 and req.read_ratio > 0:
        compute_cost_per_sec = (req.app_vcpu * CLOUD_RUN_VCPU_SEC_DEFAULT + gib * CLOUD_RUN_MEM_GIB_SEC_DEFAULT)
        compute_cost_per_req = (req.avg_latency_ms / 1000.0) * compute_cost_per_sec
        req_cost_per_req = CLOUD_RUN_REQ_PER_MILLION_DEFAULT / 1_000_000.0
        egress_cost_per_req = (req.avg_response_kb / (1024.0 * 1024.0)) * EGRESS_COST_PER_GB_USD
        total_variable_cost_per_req = compute_cost_per_req + req_cost_per_req + egress_cost_per_req
        if total_variable_cost_per_req > 0:
            crossover_rps = round(baseline_fargate_app_usd / (active_seconds * total_variable_cost_per_req), 1)

    return ArbitrageResult(
        read_weight_target=round(read_weight_target, 2),
        read_weight_source=round(read_weight_source, 2),
        write_destination="SOURCE",
        active_master="SOURCE",
        lag_fenced=lag_fenced,
        baseline_monthly_cost=_calc_fx(baseline_fargate_app_usd, req.currency, req.custom_fx_rate),
        arbitrage_monthly_cost=_calc_fx(arbitrage_monthly_cost_usd, req.currency, req.custom_fx_rate),
        monthly_savings=_calc_fx(monthly_savings_usd, req.currency, req.custom_fx_rate),
        savings_percent=savings_pct,
        cross_cloud_egress_monthly_usd=round(cross_cloud_egress_usd, 2),
        currency=req.currency.upper(),
        fx_rate=fx_rate,
        crossover_rps=crossover_rps,
        recommendation=recommendation,
        reasons=reasons,
    )
