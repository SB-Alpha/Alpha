"""finops 모듈 계약 (담당 박재현, 첫 구현 이주호). 다른 모듈은 이 파일만 import 합니다.

역할 나누기 (슬랙 11/7 합의):
- 숫자(단가·비용·절감률)는 계산 코드가 냅니다. 비용 산출은 estimate.py(클라우드 공식 가격 API).
- 금액·증감·추천이 들어가는 문장은 코드가 씁니다. AI 는 숫자 없는 설명 문장만 주의점 뒤에 덧붙입니다 (이주호).

`FinopsInput` 은 estimate.py 가 만들고, 수동 입력이나 다른 계산도 같은 형식으로 넣을 수 있습니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 클라우드 이름과 항목 이름은 코드가 쓰는 결론 문장에 그대로 들어갑니다. 자유 문장이 들어오면
# "가장 많이 아끼는 후보는 gcp (월 90% 확정 절감) 입니다" 처럼 결론을 꾸밀 수 있어 모양을 막습니다.
CLOUD_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # 예: aws, gcp, azure, on-prem
ITEM_NAME = r"^[^\d%$]{1,40}$"  # 숫자·%·$ 없이 40자까지. 금액과 비율은 코드만 씁니다


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


class Rightsizing(BaseModel):
    """크기 줄이기 제안 하나. 근거 지표는 계산 코드가 문장으로 넣습니다."""

    resource: str  # 예: "앱 CPU", "앱 메모리", "DB 등급"
    current: str  # 예: "1 vCPU"
    suggested: str  # 예: "0.5 vCPU"
    evidence: str  # 예: "14일 CPU 사용률 p95 12%"


class FinopsInput(BaseModel):
    run_id: str | None = None
    currency: Literal["USD"] = "USD"
    # 사용량을 본 기간. 모르면 None. True·"30" 같은 값이 정수로 바뀌어 통과하지 않게 strict
    period_days: int | None = Field(default=None, ge=1, strict=True)
    current: CloudCost  # 지금 원본
    candidates: list[CloudCost] = Field(default_factory=list)  # 이전 후보 (예: gcp, azure)
    rightsizing: list[Rightsizing] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)  # 계산에 쓴 가정. 규칙 주의점에 그대로 실립니다


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


class CostRequest(BaseModel):
    """비용을 낼 때 필요한 크기. assessment 결과에서 채웁니다. 모르면 None."""

    source_region: str  # 원본 AWS 리전. 예: "ap-northeast-2"
    app_vcpu: float | None = None
    app_memory_mib: int | None = None
    app_instances: int = Field(default=1, ge=1)  # 떠 있는 인스턴스(복제본) 수. assessment 가 아직 안 읽음
    db_instance_class: str | None = None  # 원본 RDS 등급. 예: "db.t3.micro"
    db_vcpu: int | None = None
    db_memory_gib: float | None = None
    db_storage_gb: int | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    # None 이면 앱이 한 달 내내 켜져 있다고 봅니다. 주면 Cloud Run 은 요청 없는 시간에 0대, Azure 는 유휴 요금
    traffic: Traffic | None = None


class FinopsEstimate(BaseModel):
    """POST /finops/estimate 결과: 가격 API 로 만든 비용 항목(input)과 그 비교(report)."""

    input: FinopsInput
    report: FinopsReport
