"""AI 보강: 비용 비교 결과에 승인자가 알아야 할 설명을 덧붙입니다. (이주호)

- 기본은 꺼져 있습니다. ALPHA_AI_ENABLED=1 일 때만 Claude 를 부릅니다 (assessment AI 와 같은 스위치).
- 금액·증감·추천 문장(summary)과 savings · best 는 코드가 씁니다. AI 는 그 문장을 바꾸지 못합니다.
- AI 는 주의점 뒤에 붙일 설명만 씁니다. 숫자가 있거나 비용 판단·추천을 하는 설명은 버립니다.
  입력에 있는 숫자인지 대조하는 방식은 "$15 를 $60 으로" 같은 틀린 조합을 못 막아서, 숫자 자체를 막습니다.
- 꺼져 있거나, SDK 가 없거나, 호출·검증이 실패해도 규칙 결과를 그대로 돌려줍니다.
"""

from __future__ import annotations

import json
import logging
import os
import re

from pydantic import BaseModel, Field

from .schemas import FinopsInput, FinopsReport, judgement_in

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
# 설명 문장만 쓰는 일이라 생각을 짧게 합니다. Sonnet 5.5 는 thinking 을 끌 수 없고, thinking 도
# max_tokens 에 포함되므로 답보다 넉넉하게 잡습니다 (작으면 stop_reason=max_tokens 로 규칙 문장으로 돌아감).
EFFORT = "low"
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 60.0
MAX_SENTENCES = 5

SYSTEM = (
    "너는 클라우드 이전 비용 비교를 승인자에게 설명하는 도우미다. 금액·증감·추천 문장은 계산 코드가 이미 썼다 "
    "(입력의 rule_summary · rule_notes).\n"
    "- 숫자를 쓰지 마라. 금액·비율·기간·개수 모두 쓰지 마라.\n"
    "- 어느 후보가 싸다·비싸다·아낀다·추천한다 같은 판단을 하지 마라.\n"
    "- notes 에 승인자가 확인할 점을 한국어 문장 1~5개로 써라: 빠진 항목이 왜 중요한지, 비용 근거의 한계, "
    "크기 줄이기 제안을 적용할 때의 위험.\n"
    "- 입력에 없는 사실을 지어내지 마라."
)

_DIGIT = re.compile(r"\d")
# 비용 판단·추천 표현 목록은 schemas.JUDGEMENT_WORDS 하나로 둡니다 (크기 제안 문장도 같은 목록으로 검사)


class AiNotes(BaseModel):
    notes: list[str] = Field(default_factory=list)


def enabled() -> bool:
    return os.environ.get("ALPHA_AI_ENABLED") == "1"


def enhance(inp: FinopsInput, report: FinopsReport) -> FinopsReport:
    if not enabled():
        return report
    try:
        notes = _ask_claude(_facts_for_prompt(inp, report))
    except Exception as e:  # 어떤 실패든 규칙 결과로 돌아감
        log.warning("finops AI 보강 실패, 규칙 결과 사용 (%s)", type(e).__name__)
        return report
    if notes is None:
        return report
    problem = check_notes(notes)
    if problem:
        log.warning("finops AI 설명을 버림 (%s), 규칙 결과 사용", problem)
        return report
    # 규칙 주의점(빠진 항목·근거 기간)은 지우지 않고, AI 설명은 그 뒤에 붙입니다
    return report.model_copy(update={
        "notes": report.notes + [n.strip() for n in notes.notes],
        "summary_source": "ai",
    })


def check_notes(notes: AiNotes) -> str | None:
    """쓸 수 없는 설명이면 이유를, 쓸 수 있으면 None 을 돌려줍니다."""
    if not 1 <= len(notes.notes) <= MAX_SENTENCES or not all(n.strip() for n in notes.notes):
        return "sentence_count"
    for note in notes.notes:
        if _DIGIT.search(note):
            return "number"
        if judgement_in(note):
            return "cost_judgement"
    return None


def _facts_for_prompt(inp: FinopsInput, report: FinopsReport) -> str:
    data = {
        "input": inp.model_dump(mode="json", exclude={"run_id"}),
        "computed": {"savings": [s.model_dump(mode="json") for s in report.savings], "best": report.best},
        "rule_summary": report.summary,
        "rule_notes": report.notes,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _ask_claude(facts: str) -> AiNotes | None:
    import anthropic  # 선택 의존성: pip install -e ".[ai]"

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": facts}],
            output_config={"effort": EFFORT},
            output_format=AiNotes,
        )
    except anthropic.RateLimitError:
        log.warning("finops AI: 요청 한도 초과, 규칙 결과 사용")
        return None
    except anthropic.APIStatusError as e:
        log.warning("finops AI: API 오류 %s, 규칙 결과 사용", e.status_code)
        return None
    except anthropic.APIConnectionError:
        log.warning("finops AI: 연결 실패, 규칙 결과 사용")
        return None

    if response.stop_reason != "end_turn":  # refusal · max_tokens 등은 쓰지 않음
        log.warning("finops AI: stop_reason=%s, 규칙 결과 사용", response.stop_reason)
        return None
    return response.parsed_output
