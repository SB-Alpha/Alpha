"""AI 보강: 승인 화면용 요약 3줄을 다듬고 설명용 위험을 덧붙입니다. (이주호)

- 기본은 꺼져 있습니다. 환경 변수 ALPHA_AI_ENABLED=1 일 때만 Claude 를 부릅니다 (팀 합의 전).
- 판정(checks · approvable · blocking)과 target_infra 는 절대 바꾸지 않습니다. AI 결과는 문장만 씁니다.
- 꺼져 있거나, SDK 가 없거나, 호출·검증이 실패하면 규칙 기반 plan 을 그대로 돌려줍니다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Literal

from pydantic import BaseModel, Field

from .schemas import MigrationPlan, Risk

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 30.0
MAX_AI_RISKS = 5
AWS_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")

SYSTEM = (
    "너는 AWS → GCP 이전 계획을 승인자에게 설명하는 도우미다. 입력 JSON 은 규칙 코드가 이미 판정한 결과다.\n"
    "- 판정을 바꾸지 마라. approvable 이 false 면 요약에 승인 불가와 그 이유를 분명히 써라.\n"
    "- 입력에 없는 값·수치를 지어내지 마라. 모르는 것은 '미제공' 이라고 써라.\n"
    "- summary 는 한국어 문장 정확히 3개: 무엇을 어디로 옮기는지 / 필수 조건 결과 / 승인하면 다음에 일어나는 일 또는 막힌 이유.\n"
    "- risk_notes 는 입력 risks 에 없는, 승인자가 알아야 할 운영상 주의점만 최대 5개. 없으면 빈 목록."
)


class AiRiskNote(BaseModel):
    level: Literal["high", "medium", "low"]
    title: str
    detail: str


class AiNotes(BaseModel):
    summary: list[str]
    risk_notes: list[AiRiskNote] = Field(default_factory=list)


def enabled() -> bool:
    return os.environ.get("ALPHA_AI_ENABLED") == "1"


def enhance(plan: MigrationPlan) -> MigrationPlan:
    if not enabled():
        return plan
    try:
        notes = _ask_claude(plan)
    except Exception as e:  # 어떤 실패든 규칙 결과로 돌아감
        log.warning("AI 보강 실패, 규칙 결과 사용 (%s)", type(e).__name__)
        return plan
    if notes is None or not _valid(notes):
        return plan

    extra = [Risk(level=n.level, title=n.title.strip(), detail=n.detail.strip(), source="ai")
             for n in notes.risk_notes[:MAX_AI_RISKS]]
    return plan.model_copy(update={
        "summary": [s.strip() for s in notes.summary],
        "summary_source": "ai",
        "risks": plan.risks + extra,
    })


def _valid(notes: AiNotes) -> bool:
    return len(notes.summary) == 3 and all(s.strip() for s in notes.summary)


def _plan_for_prompt(plan: MigrationPlan) -> str:
    # 판단에 필요한 것만 보냅니다. env_vars 값은 보내지 않고 이름만.
    data = plan.model_dump(mode="json", exclude={"generated_at", "summary", "summary_source"})
    infra = data["target_infra"]
    infra["env_vars"] = sorted(infra.get("env_vars", {}))
    # 판단에 필요 없는 AWS 계정 ID(이미지 주소 등)는 가려서 보냅니다
    return AWS_ACCOUNT.sub("<aws-account>", json.dumps(data, ensure_ascii=False, sort_keys=True))


def _ask_claude(plan: MigrationPlan) -> AiNotes | None:
    import anthropic  # 선택 의존성: pip install -e ".[ai]"

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": _plan_for_prompt(plan)}],
            output_format=AiNotes,
        )
    except anthropic.RateLimitError:
        log.warning("AI 보강: 요청 한도 초과, 규칙 결과 사용")
        return None
    except anthropic.APIStatusError as e:
        log.warning("AI 보강: API 오류 %s, 규칙 결과 사용", e.status_code)
        return None
    except anthropic.APIConnectionError:
        log.warning("AI 보강: 연결 실패, 규칙 결과 사용")
        return None

    if response.stop_reason != "end_turn":  # refusal · max_tokens 등은 쓰지 않음
        log.warning("AI 보강: stop_reason=%s, 규칙 결과 사용", response.stop_reason)
        return None
    return response.parsed_output
