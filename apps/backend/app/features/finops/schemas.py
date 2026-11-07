"""finops 모듈 계약 (초안, 박준서·이주호). 다른 모듈은 이 파일만 import 합니다.

역할 나누기 (슬랙 11/7 합의):
- 숫자(사용량·단가·비용·절감률)는 계산 코드가 냅니다. 비용 계산(CloudWatch·가격 API·Infracost)은 박준서님.
- 승인자가 읽을 문장은 규칙 코드가 씁니다 (이주호). AI 는 ai.py 의 정해진 주의점 코드만 고르고,
  코드가 조건을 확인한 뒤 고정 문장을 notes 뒤에 붙입니다. AI 는 숫자나 문장을 만들지 못합니다.

`FinopsInput` 은 박준서님 계산 결과를 받을 자리입니다. 형식이 확정되면 맞춥니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CostLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=120)  # 예: "앱 컴퓨팅", "DB", "스토리지"
    # 모르면 None. 합계에서 빠지고 "미제공" 으로 표시. 음수·Infinity·NaN 은 거절
    monthly_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # 숫자 출처. 예: "aws-pricing-api", "infracost", "cloudwatch", "manual"
    source: str = Field(min_length=1, max_length=120)

    @field_validator("monthly_usd", mode="before")
    @classmethod
    def _money_must_be_number(cls, value: object) -> object:
        # 문자열·불리언은 float 로 바뀌지 않게 먼저 막습니다. 오류 문장에 입력값을 넣지 않습니다
        if isinstance(value, (bool, str)):
            raise ValueError("금액은 숫자 또는 null 이어야 합니다")
        return value


class CloudCost(BaseModel):
    """한 클라우드(현재 원본 또는 이전 후보)의 월 비용."""

    model_config = ConfigDict(extra="forbid")

    # 예: "aws", "gcp", "azure", "onprem". 공백만 있는 이름도 거절
    cloud: str = Field(min_length=1, max_length=40, pattern=r"\S")
    region: str | None = Field(default=None, max_length=40)
    lines: list[CostLine] = Field(default_factory=list, max_length=50)


class Rightsizing(BaseModel):
    """크기 줄이기 제안 하나. 근거 문장은 입력값이라 notes 에 그대로 옮기지 않습니다."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=200)  # 예: "앱 CPU", "앱 메모리", "DB 등급"
    current: str = Field(min_length=1, max_length=200)  # 예: "1 vCPU"
    suggested: str = Field(min_length=1, max_length=200)  # 예: "0.5 vCPU"
    evidence: str = Field(min_length=1, max_length=200)  # 예: "14일 CPU 사용률 p95 12%"


class FinopsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, max_length=120)
    currency: Literal["USD"] = "USD"
    period_days: int | None = Field(default=None, gt=0)  # 사용량을 본 기간. 모르면 None
    current: CloudCost  # 지금 원본
    candidates: list[CloudCost] = Field(default_factory=list, max_length=10)  # 이전 후보 (예: gcp, azure)
    rightsizing: list[Rightsizing] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _candidates_must_be_distinct(self) -> FinopsInput:
        names = [c.cloud.strip().casefold() for c in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError("이전 후보 클라우드가 중복됩니다")
        if self.current.cloud.strip().casefold() in names:
            raise ValueError("이전 후보가 현재 클라우드와 같습니다")
        return self


class Saving(BaseModel):
    """후보 하나의 절감 계산 결과. 전부 계산 코드가 냅니다."""

    cloud: str
    current_monthly_usd: float | None
    candidate_monthly_usd: float | None  # 아는 항목만 더한 합계 (complete 가 False 면 일부)
    monthly_saving_usd: float | None  # 양수면 절감, 음수면 증가. complete 가 False 면 None
    saving_pct: float | None  # 소수 1자리. 현재 비용이 0 이거나 모르거나 계산이 넘치면 None
    complete: bool  # 양쪽 모든 항목에 금액이 있고 비교 항목(개수 포함)이 같으면 True. False 면 비교하지 않음


class FinopsReport(BaseModel):
    savings: list[Saving]
    best: str | None  # 완전한 합계 중 가장 많이 아끼는 후보. 없거나 동률이면 None
    summary: list[str]  # 승인자용 문장
    notes: list[str] = Field(default_factory=list)  # 주의점. AI 가 켜져 있으면 고정 문장이 뒤에 붙을 수 있음
    # 항상 "rules". AI 는 summary 를 쓰지 않습니다. 계약을 바꾸지 않으려고 필드와 타입은 그대로 둡니다
    summary_source: Literal["rules", "ai"] = "rules"
