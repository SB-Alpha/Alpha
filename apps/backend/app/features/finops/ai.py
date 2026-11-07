"""AI 보강: 비용 비교 결과를 승인자가 읽을 제안 문장으로 다듬습니다. (이주호)

- 기본은 꺼져 있습니다. ALPHA_AI_ENABLED=1 일 때만 Claude 를 부릅니다 (assessment AI 와 같은 스위치).
- AI 는 문장(summary · notes)만 씁니다. savings · best 같은 숫자 결과는 절대 바꾸지 않습니다.
- **숫자 대조:** AI 문장에 나온 숫자가 입력이나 계산 결과에 없으면 그 결과는 버리고 규칙 문장을 씁니다.
- 꺼져 있거나, SDK 가 없거나, 호출·검증이 실패해도 규칙 문장을 그대로 돌려줍니다.
"""

from __future__ import annotations

import json
import logging
import os
import re

from pydantic import BaseModel, Field

from .schemas import FinopsInput, FinopsReport

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
# 설명 문장만 쓰는 일이라 생각을 짧게 합니다. Sonnet 5.5 는 thinking 을 끌 수 없고, thinking 도
# max_tokens 에 포함되므로 답보다 넉넉하게 잡습니다 (작으면 stop_reason=max_tokens 로 규칙 문장으로 돌아감).
EFFORT = "low"
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 60.0
MAX_SENTENCES = 5

SYSTEM = (
    "너는 클라우드 이전 비용 비교를 승인자에게 설명하는 도우미다. 입력 JSON 의 숫자는 계산 코드가 이미 낸 값이다.\n"
    "- 숫자를 새로 계산하거나 바꾸지 마라. 문장에 쓰는 숫자는 입력 JSON 에 있는 값을 그대로 옮겨라.\n"
    "- 입력에 없는 값·단가·기간을 지어내지 마라. 모르는 것은 '미제공' 이라고 써라.\n"
    "- summary 는 한국어 문장 2~5개: 현재 비용 / 후보별 증감 / 추천 후보 또는 추천할 수 없는 이유.\n"
    "- notes 는 승인자가 알아야 할 주의점(빠진 항목, 근거 기간, 크기 줄이기 제안의 위험) 최대 5개."
)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class AiNotes(BaseModel):
    summary: list[str]
    notes: list[str] = Field(default_factory=list)


def enabled() -> bool:
    return os.environ.get("ALPHA_AI_ENABLED") == "1"


def enhance(inp: FinopsInput, report: FinopsReport) -> FinopsReport:
    if not enabled():
        return report
    facts = _facts_for_prompt(inp, report)
    try:
        notes = _ask_claude(facts)
    except Exception as e:  # 어떤 실패든 규칙 결과로 돌아감
        log.warning("finops AI 보강 실패, 규칙 결과 사용 (%s)", type(e).__name__)
        return report
    if notes is None or not _valid(notes):
        return report
    unknown = unknown_numbers(notes.summary + notes.notes, facts)
    if unknown:
        log.warning("finops AI 문장에 입력에 없는 숫자 %d개, 규칙 결과 사용", len(unknown))
        return report
    return report.model_copy(update={
        "summary": [s.strip() for s in notes.summary],
        "notes": [n.strip() for n in notes.notes[:MAX_SENTENCES]],
        "summary_source": "ai",
    })


def _valid(notes: AiNotes) -> bool:
    return 2 <= len(notes.summary) <= MAX_SENTENCES and all(s.strip() for s in notes.summary)


def _facts_for_prompt(inp: FinopsInput, report: FinopsReport) -> str:
    data = {
        "input": inp.model_dump(mode="json", exclude={"run_id"}),
        "computed": {"savings": [s.model_dump(mode="json") for s in report.savings], "best": report.best},
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _as_number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def unknown_numbers(sentences: list[str], facts: str) -> list[str]:
    """문장 속 숫자 중 facts(JSON) 에 없는 것. 반올림 표기(소수 0~2자리)와 천 단위 쉼표는 같은 숫자로 봅니다."""
    allowed: set[float] = set()
    for token in _NUMBER.findall(facts):
        value = _as_number(token)
        if value is None:
            continue
        for digits in (0, 1, 2):
            allowed.add(round(value, digits))
        allowed.add(abs(value))
    found = []
    for sentence in sentences:
        for token in _NUMBER.findall(sentence):
            value = _as_number(token)
            if value is None or value not in allowed:
                found.append(token)
    return found


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
