"""finops 모듈 계약 (담당 박재현, 첫 구현 이주호). 다른 모듈은 이 파일만 import 합니다.

역할 나누기 (슬랙 11/7 합의):
- 숫자(단가·비용·절감률)는 계산 코드가 냅니다. 비용 산출은 estimate.py(클라우드 공식 가격 API).
- 금액·증감·추천이 들어가는 문장은 코드가 씁니다. AI 는 숫자 없는 설명 문장만 주의점 뒤에 덧붙입니다 (이주호).

`FinopsInput` 은 estimate.py 가 만들고, 수동 입력이나 다른 계산도 같은 형식으로 넣을 수 있습니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 클라우드 이름과 항목 이름은 코드가 쓰는 결론 문장에 그대로 들어갑니다. 자유 문장이 들어오면
# "가장 많이 아끼는 후보는 gcp (월 90% 확정 절감) 입니다" 처럼 결론을 꾸밀 수 있어 모양을 막습니다.
CLOUD_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # 예: aws, gcp, azure, on-prem
ITEM_NAME = r"^[^\d%$]{1,40}$"  # 숫자·%·$ 없이 40자까지. 금액과 비율은 코드만 씁니다
SOURCE_CLOUD = "aws"  # /estimate 원본 클라우드 기본값 (CostRequest.source_cloud 로 바꿀 수 있음)
SourceCloud = Literal["aws", "gcp", "azure"]

# 크기 상한. 가장 큰 단일 인스턴스보다 넉넉한 값이고, 곱셈이 넘치거나 오타로 큰 값이 들어오는 것만 막습니다
MAX_APP_VCPU = 64
MAX_APP_MEMORY_MIB = 512 * 1024
MAX_APP_INSTANCES = 1000

# 읽기 분산을 막는 복제 지연 기준. 전환 승인 ② 판정(workflows/readiness.py 의 MAX_REPLICATION_LAG_BYTES)과
# 같은 값을 씁니다. 다른 화면과 판단이 어긋나지 않게 테스트가 두 값이 같은지 확인합니다
MAX_SAFE_LAG_BYTES = 1024 * 1024
MAX_DB_VCPU = 128
MAX_DB_MEMORY_GIB = 4096
MAX_DB_STORAGE_GB = 64 * 1024

# 숫자 없이도 비용 판단·추천이 되는 표현. 이런 문장은 코드(service.rule_summary)만 씁니다.
# AI 설명(ai.check_notes)과 크기 제안 문장(Rightsizing)이 같은 목록으로 검사받습니다.
JUDGEMENT_WORDS = ("추천", "권장", "저렴", "비싸", "싸게", "싸다", "싼 ", "절감", "아끼", "아낄", "아낍", "줄어", "늘어",
                   "이득", "손해", "선택하", "recommend", "cheap", "expensive", "saving")


def judgement_in(text: str) -> str | None:
    lowered = text.lower()
    return next((word for word in JUDGEMENT_WORDS if word in lowered), None)


def _unique_clouds(clouds: list[str], source: str) -> None:
    if source in clouds:
        raise ValueError(f"후보에 원본과 같은 클라우드({source})가 있습니다")
    dupes = sorted({c for c in clouds if clouds.count(c) > 1})
    if dupes:
        raise ValueError(f"같은 후보 클라우드가 두 번 이상 있습니다: {', '.join(dupes)}")


class CostLine(BaseModel):
    item: str = Field(pattern=ITEM_NAME)  # 예: "앱 컴퓨팅", "DB", "스토리지". 원본과 후보가 같은 이름을 써야 항목끼리 맞춰집니다
    # 모르면 None. NaN·무한대·음수는 계산을 망가뜨리므로 입력에서 거절합니다
    monthly_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source: str  # 숫자 출처. 예: "aws-pricing-api", "infracost", "cloudwatch", "manual"


class CloudCost(BaseModel):
    """한 클라우드(현재 원본 또는 이전 후보)의 월 비용."""

    cloud: str = Field(pattern=CLOUD_SLUG, max_length=20)  # 예: "aws", "gcp", "azure", "on-prem"
    region: str | None = None
    lines: list[CostLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_line_per_item(self) -> CloudCost:
        # 항목은 이름으로 맞춥니다(service.missing_items). 같은 이름이 두 줄이면 합계만 부풀어 가짜 절감이 생깁니다.
        names = [line.item.strip() for line in self.lines]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"{self.cloud} 에 같은 항목이 두 번 이상 있습니다: {', '.join(dupes)}")
        return self


class Rightsizing(BaseModel):
    """크기 줄이기 제안 하나. 근거 지표는 계산 코드가 문장으로 넣습니다.

    네 칸 모두 규칙 주의점에 그대로 실리므로 AI 설명과 같은 판단 금지어 검사를 거칩니다.
    근거에는 측정값이 들어가야 하므로 숫자는 막지 않습니다.
    """

    resource: str = Field(max_length=40)  # 예: "앱 CPU", "앱 메모리", "DB 등급"
    current: str = Field(max_length=40)  # 예: "1 vCPU"
    suggested: str = Field(max_length=40)  # 예: "0.5 vCPU"
    evidence: str = Field(max_length=120)  # 예: "14일 CPU 사용률 p95 12%"

    @field_validator("resource", "current", "suggested", "evidence")
    @classmethod
    def _no_cost_judgement(cls, value: str) -> str:
        word = judgement_in(value)
        if word:
            raise ValueError(f"비용 판단·추천 표현({word!r})은 계산 코드만 씁니다")
        return value


class Crossover(BaseModel):
    """Cloud Run(요청 없는 시간 0대)이 Fargate(내내 켜짐)보다 싸지는 지점. 앱 컴퓨팅만 비교합니다 (DB 는 트래픽과 무관).

    전부 estimate.py 가 단가로 계산합니다. 계산할 수 없으면 None 이고 reason 에 이유를 적습니다.
    """

    fargate_app_monthly_usd: float | None  # 원본 Fargate 앱, 내내 켜짐
    cloud_run_instance_monthly_usd: float | None  # Cloud Run 1대를 한 달 내내 켰을 때
    # 유휴 시간 비율이 이 값보다 크면 Cloud Run 이 쌉니다. 0 이면 내내 켜져 있어도 Cloud Run 이 쌉니다
    break_even_idle_percent: float | None
    # 지금 유휴 비율에서, 요청 있는 시간 평균 RPS 가 이 값 이하이면 Cloud Run 이 쌉니다
    break_even_rps: float | None
    concurrency: int  # Cloud Run 인스턴스 하나가 동시에 받는 요청 수 (가정)
    max_instances: int  # Terraform max_instance_count
    reason: str | None = None  # break_even_rps 가 None 인 이유


class FinopsInput(BaseModel):
    run_id: str | None = None
    currency: Literal["USD"] = "USD"
    # 사용량을 본 기간. 모르면 None. True·"30" 같은 값이 정수로 바뀌어 통과하지 않게 strict
    period_days: int | None = Field(default=None, ge=1, strict=True)
    current: CloudCost  # 지금 원본
    candidates: list[CloudCost] = Field(default_factory=list)  # 이전 후보 (예: gcp, azure)
    rightsizing: list[Rightsizing] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)  # 계산에 쓴 가정. 규칙 주의점에 그대로 실립니다
    crossover: Crossover | None = None  # estimate.py 가 GCP 후보가 있을 때 채웁니다. 수동 입력은 None

    @model_validator(mode="after")
    def _candidates_differ(self) -> FinopsInput:
        # 원본과 같은 클라우드를 후보로 넣으면 "aws 로 옮기면 … 줄어듭니다" 같은 문장이 나옵니다
        _unique_clouds([c.cloud for c in self.candidates], self.current.cloud)
        return self


class Saving(BaseModel):
    """후보 하나의 절감 계산 결과. 전부 계산 코드가 냅니다."""

    cloud: str
    current_monthly_usd: float | None  # 원본 모든 항목에 금액이 있을 때만. 일부만 알면 None
    candidate_monthly_usd: float | None  # 아는 항목만 더한 합계 (complete 가 False 면 일부)
    monthly_saving_usd: float | None  # 양수면 절감, 음수면 증가. 어느 쪽이든 금액이 빠진 항목이 있으면 None
    saving_pct: float | None  # 소수 1자리. 현재 비용이 0 이거나 모르면 None
    complete: bool  # 양쪽 모든 항목에 금액이 있고 후보에 원본 항목이 다 있으면 True
    missing: list[str] = Field(default_factory=list)  # 금액이 없거나 후보에 없는 항목. 예: "azure DB"


class FinopsReport(BaseModel):
    savings: list[Saving]
    best: str | None  # 완전한 합계 중 가장 많이 아끼는 후보. 없으면 None
    summary: list[str]  # 승인자용 문장. 항상 코드가 씁니다
    notes: list[str] = Field(default_factory=list)  # 주의점. 규칙 주의점이 먼저, AI 설명은 그 뒤
    summary_source: Literal["rules", "ai"] = "rules"  # "ai" 면 notes 끝에 AI 설명이 붙었다는 뜻


# -- 비용 산출 입력 (estimate.py: 크기 × 가격 API 단가 → FinopsInput) -----------------------------------


class Candidate(BaseModel):
    cloud: str = Field(pattern=CLOUD_SLUG, max_length=20)  # "gcp" | "azure"
    region: str  # 예: "asia-northeast3", "koreacentral"


class Traffic(BaseModel):
    """트래픽 가정. 실측이 아니라 호출하는 쪽(예: 화면 슬라이더)이 넣는 값입니다."""

    idle_percent: float = Field(ge=0, le=95, allow_inf_nan=False)  # 한 달 중 요청이 없는 시간 비율
    rps: float = Field(default=0, ge=0, le=100_000, allow_inf_nan=False)  # 요청이 있는 시간의 초당 평균 요청 수
    # 요청 하나를 처리하는 평균 시간. RPS 교차점(필요한 Cloud Run 인스턴스 수)에만 씁니다. 모르면 None
    avg_latency_ms: float | None = Field(default=None, gt=0, le=60_000, allow_inf_nan=False)
    # 읽기 요청 비율(0~1)과 평균 응답 크기(KB). 차익거래에만 씁니다. 모르면 None 이고 차익거래를 내지 않습니다 (기본값으로 채우지 않음)
    read_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    avg_response_kb: float | None = Field(default=None, gt=0, le=10_000, allow_inf_nan=False)
    # 실행에서 잰 복제 지연(바이트). 모르면 None 이고, 차익거래는 지연 안전 판단을 하지 않았다고 적습니다 (0 으로 채우지 않음)
    replication_lag_bytes: int | None = Field(default=None, ge=0)
    max_safe_lag_bytes: int = Field(default=MAX_SAFE_LAG_BYTES, ge=0)


class CostRequest(BaseModel):
    """비용을 낼 때 필요한 크기. assessment 결과에서 채웁니다. 모르면 None."""

    # 크기는 모르면 None("미제공") 그대로 둡니다. 주면 0 보다 크고 상한 이하여야 합니다.
    # 상한은 클라우드 한 대가 실제로 낼 수 있는 크기보다 넉넉하게 잡아, 곱셈이 넘치는 값만 막습니다.
    source_cloud: SourceCloud = SOURCE_CLOUD  # 지금 원본 클라우드. 원본 비용은 이 클라우드의 가격 API 로 냅니다
    source_region: str  # 원본 리전. 예: "ap-northeast-2", "asia-northeast3", "koreacentral"
    app_vcpu: float | None = Field(default=None, gt=0, le=MAX_APP_VCPU, allow_inf_nan=False)  # 원본 앱 크기
    app_memory_mib: int | None = Field(default=None, gt=0, le=MAX_APP_MEMORY_MIB)
    # 후보에 띄울 앱 크기 (예: 계획의 목적지 크기). 안 주면 원본과 같은 크기로 셉니다. 원본 크기를 모를 때
    # 목적지 크기를 원본 크기로 바꿔 넣지 않게 따로 받습니다.
    target_app_vcpu: float | None = Field(default=None, gt=0, le=MAX_APP_VCPU, allow_inf_nan=False)
    target_app_memory_mib: int | None = Field(default=None, gt=0, le=MAX_APP_MEMORY_MIB)
    # 떠 있는 인스턴스(복제본) 수. assessment 가 아직 안 읽어 모르면 None 이고, 1대로 계산했다고 가정에 적습니다
    app_instances: int | None = Field(default=None, ge=1, le=MAX_APP_INSTANCES)
    db_instance_class: str | None = None  # 원본 RDS 등급. 예: "db.t3.micro"
    db_vcpu: int | None = Field(default=None, gt=0, le=MAX_DB_VCPU)
    db_memory_gib: float | None = Field(default=None, gt=0, le=MAX_DB_MEMORY_GIB, allow_inf_nan=False)
    db_storage_gb: int | None = Field(default=None, gt=0, le=MAX_DB_STORAGE_GB)
    candidates: list[Candidate] = Field(default_factory=list)
    # None 이면 앱이 한 달 내내 켜져 있다고 봅니다. 주면 Cloud Run 은 요청 없는 시간에 0대, Azure 는 유휴 요금
    traffic: Traffic | None = None

    @model_validator(mode="after")
    def _candidates_differ(self) -> CostRequest:
        # 가격 API 를 부르기 전에 막습니다. build_input 안에서 FinopsInput 검사에 걸리면 422 가 아니라 500 이 됩니다
        _unique_clouds([c.cloud for c in self.candidates], self.source_cloud)
        return self

    @property
    def instances(self) -> int:
        """계산에 쓰는 인스턴스 수. 모르면 1대 (assumptions 가 그렇게 적습니다)."""
        return self.app_instances or 1

    def for_candidate(self) -> CostRequest:
        """후보 견적용 요청. 목적지 앱 크기를 주었으면 그 크기로 바꿉니다."""
        if self.target_app_vcpu is None and self.target_app_memory_mib is None:
            return self
        return self.model_copy(update={
            "app_vcpu": self.target_app_vcpu if self.target_app_vcpu is not None else self.app_vcpu,
            "app_memory_mib": self.target_app_memory_mib if self.target_app_memory_mib is not None else self.app_memory_mib,
        })


class FinopsEstimate(BaseModel):
    """POST /finops/estimate 결과: 가격 API 로 만든 비용 항목(input)과 그 비교(report)."""

    input: FinopsInput
    report: FinopsReport
    arbitrage: ArbitrageResult | None = None


# -- 비용 변동성 및 동적 차익거래 (Dynamic Cost-Arbitrage Routing) --------------------------
# 소프트뱅크 인프라 비용 변동성 대응: 읽기/쓰기 분리 차익거래, 환율 반영, 복제 지연 펜싱
# 환율은 표에 적어 두지 않습니다. USD 가 아니면 custom_fx_rate 를 받았을 때만 환산하고, 없으면 금액은 None 입니다.


class ArbitrageRequest(BaseModel):
    """동적 트래픽 차익거래 계산 입력."""

    source_cloud: str = Field(default=SOURCE_CLOUD, pattern=CLOUD_SLUG)
    target_cloud: str = Field(default="gcp", pattern=CLOUD_SLUG)
    # egress 단가를 조회할 리전. /estimate 는 CostRequest 의 원본 리전과 GCP 후보 리전을 넣습니다.
    # 모양 검사는 조회할 때(estimate._region) 하고, 맞지 않으면 egress 가 "미제공" 입니다 (/estimate 가 500 이 되지 않게)
    source_region: str = "ap-northeast-2"
    target_region: str = "asia-northeast3"
    app_vcpu: float = Field(default=0.5, gt=0, le=MAX_APP_VCPU, allow_inf_nan=False)
    app_memory_mib: int = Field(default=1024, gt=0, le=MAX_APP_MEMORY_MIB)
    app_instances: int = Field(default=1, ge=1, le=MAX_APP_INSTANCES)
    rps: float = Field(default=20.0, ge=0, le=100_000, allow_inf_nan=False)
    read_ratio: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)  # 읽기 트래픽 비율 (0~100%)
    idle_percent: float = Field(default=40.0, ge=0.0, le=95.0, allow_inf_nan=False)
    avg_latency_ms: float = Field(default=150.0, gt=0, le=60_000, allow_inf_nan=False)
    avg_response_kb: float = Field(default=4.0, gt=0, le=10_000, allow_inf_nan=False)
    replication_lag_bytes: int | None = Field(default=None, ge=0)  # 모르면 None. 0 은 "지연 없음"을 잰 값입니다
    max_safe_lag_bytes: int = Field(default=MAX_SAFE_LAG_BYTES, ge=0)
    read_after_write_fence_sec: float = Field(default=5.0, ge=0.0, le=300.0, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern=r"^(USD|JPY|KRW)$")
    custom_fx_rate: float | None = Field(default=None, gt=0, le=10_000_000, allow_inf_nan=False)


class ArbitrageResult(BaseModel):
    """동적 트래픽 차익거래 결과 및 추천 가중치. 금액은 요청 통화이고, 모르면 None("미제공") 입니다."""

    read_weight_target: float | None  # 목적지로 보낼 읽기 트래픽 비율 (0.0 ~ 1.0). 판단할 수 없으면 None
    read_weight_source: float | None  # 원본으로 유지할 읽기 트래픽 비율 (0.0 ~ 1.0). 판단할 수 없으면 None
    write_destination: str  # 쓰기 트래픽 처리 대상 ("SOURCE" - 항상 액티브 마스터)
    active_master: str  # 액티브 마스터 클라우드 ("SOURCE")
    lag_fenced: bool  # 복제 지연 초과로 읽기 우회가 차단되었는지 여부
    baseline_monthly_cost: float | None  # 차익거래 없이 100% 원본 가동 시 월 비용. 환율이 없으면 None
    arbitrage_monthly_cost: float | None  # 동적 차익거래 적용 시 월 비용
    monthly_savings: float | None  # 절감액
    savings_percent: float | None  # 절감률 (%)
    # 읽기 응답을 타깃이 내보낼 때 늘거나 줄어드는 인터넷 송신 요금 (USD, 음수면 줄어듦). 단가를 모르면 None
    cross_cloud_egress_monthly_usd: float | None
    egress_usd_per_gb: float | None = None  # 순 송신 단가 (타깃 - 원본, USD/GB)
    egress_source: str | None = None  # 송신 단가 출처: API 이름·조회 날짜·고른 항목 (CostLine.source 와 같은 식)
    currency: str  # 통화 코드
    fx_rate: float | None  # 적용 환율. USD 는 1.0, JPY·KRW 는 custom_fx_rate 를 받았을 때만
    crossover_rps: float | None = None  # 전체 전환 변곡점 RPS
    recommendation: str  # 운영 권고
    reasons: list[str]  # 상세 근거 및 제약 사항

