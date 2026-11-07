"""finops 모듈 계약 (초안, 박준서·이주호). 다른 모듈은 이 파일만 import 합니다.

역할 나누기 (슬랙 11/7 합의):
- 숫자(사용량·단가·비용·절감률)는 계산 코드가 냅니다. 비용 계산(CloudWatch·가격 API·Infracost)은 박준서님.
- AI 는 계산된 숫자를 받아 승인자가 읽을 제안 문장과 주의점만 씁니다 (이주호). AI 가 숫자를 만들지 않게
  ai.py 가 문장 속 숫자를 입력·계산 결과와 대조해서, 없는 숫자가 있으면 규칙 문장으로 돌아갑니다.

`FinopsInput` 은 박준서님 계산 결과를 받을 자리입니다. 형식이 확정되면 맞춥니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CostLine(BaseModel):
    item: str  # 예: "앱 컴퓨팅", "DB", "스토리지"
    monthly_usd: float | None = None  # 모르면 None. 합계에서 빠지고 "미제공" 으로 표시
    source: str  # 숫자 출처. 예: "aws-pricing-api", "infracost", "cloudwatch", "manual"


class CloudCost(BaseModel):
    """한 클라우드(현재 원본 또는 이전 후보)의 월 비용."""

    cloud: str  # 예: "aws", "gcp", "azure", "onprem"
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
    period_days: int | None = None  # 사용량을 본 기간. 모르면 None
    current: CloudCost  # 지금 원본
    candidates: list[CloudCost] = Field(default_factory=list)  # 이전 후보 (예: gcp, azure)
    rightsizing: list[Rightsizing] = Field(default_factory=list)


class Saving(BaseModel):
    """후보 하나의 절감 계산 결과. 전부 계산 코드가 냅니다."""

    cloud: str
    current_monthly_usd: float | None
    candidate_monthly_usd: float | None  # 아는 항목만 더한 합계 (complete 가 False 면 일부)
    monthly_saving_usd: float | None  # 양수면 절감, 음수면 증가. 어느 쪽이든 금액이 빠진 항목이 있으면 None
    saving_pct: float | None  # 소수 1자리. 현재 비용이 0 이거나 모르면 None
    complete: bool  # 양쪽 모든 항목에 금액이 있으면 True. False 면 일부 항목이 빠진 합계


class FinopsReport(BaseModel):
    savings: list[Saving]
    best: str | None  # 완전한 합계 중 가장 많이 아끼는 후보. 없으면 None
    summary: list[str]  # 승인자용 문장
    notes: list[str] = Field(default_factory=list)  # 주의점
    summary_source: Literal["rules", "ai"] = "rules"
