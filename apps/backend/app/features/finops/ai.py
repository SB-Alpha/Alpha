"""AI 보강: 비용 비교 결과에 붙일 주의점 코드를 고릅니다. (이주호)

- 기본은 꺼져 있습니다. ALPHA_AI_ENABLED=1 일 때만 Claude 를 부릅니다 (assessment AI 와 같은 스위치).
- AI 는 정해진 코드(CaveatCode)만 고릅니다. 문장은 코드가 씁니다. 자유 문장은 모델 계약에서 거절됩니다.
- 코드가 고른 주의점은 조건이 맞을 때만 notes 뒤에 붙습니다. summary · savings · best 는 바꾸지 않습니다.
- AI 가 summary 를 쓰지 않으므로 summary_source 는 항상 "rules" 입니다.
- 모델에는 숫자와 플래그만 보냅니다. 항목 이름 · 출처 · 근거 · 클라우드 · 리전 문자열은 보내지 않습니다.
- 꺼져 있거나, SDK 가 없거나, 호출이 실패해도 규칙 결과를 그대로 돌려줍니다.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import FinopsInput, FinopsReport

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 30.0

CaveatCode = Literal["INCOMPLETE_COMPARISON", "NO_USAGE_PERIOD", "COST_INCREASE", "NO_CONFIRMED_SAVING"]

SYSTEM = (
    "너는 클라우드 이전 비용 비교에 붙일 주의점 코드를 고르는 도우미다. 입력 JSON 의 숫자는 계산 코드가 이미 낸 값이다.\n"
    "- 아래 코드 중 입력에 해당하는 것만 caveats 에 담아라. 해당하는 것이 없으면 빈 목록을 돌려줘라.\n"
    "- 문장을 쓰지 마라. 코드 말고 다른 값은 넣지 마라.\n"
    "- INCOMPLETE_COMPARISON: complete 가 false 인 후보가 있다.\n"
    "- NO_USAGE_PERIOD: period_days 가 null 이다.\n"
    "- COST_INCREASE: monthly_saving_usd 가 음수인 후보가 있다.\n"
    "- NO_CONFIRMED_SAVING: has_best 가 false 이고 monthly_saving_usd 가 양수인 후보가 없다."
)


class AiNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caveats: list[CaveatCode] = Field(default_factory=list, max_length=4)


def _any_incomplete(inp: FinopsInput, report: FinopsReport) -> bool:
    return any(not s.complete for s in report.savings)


def _no_usage_period(inp: FinopsInput, report: FinopsReport) -> bool:
    return inp.period_days is None


def _any_increase(inp: FinopsInput, report: FinopsReport) -> bool:
    return any(s.monthly_saving_usd is not None and s.monthly_saving_usd < 0 for s in report.savings)


def _no_confirmed_saving(inp: FinopsInput, report: FinopsReport) -> bool:
    # 동률이면 best 는 None 이지만 후보는 실제로 줄어듭니다. 이때 '줄어드는 후보가 없다' 고 쓰면 거짓입니다
    has_saving = any(s.monthly_saving_usd is not None and s.monthly_saving_usd > 0 for s in report.savings)
    return report.best is None and not has_saving


# 코드 → (조건, 문장). 문장에는 숫자와 클라우드 이름을 넣지 않습니다
_CAVEATS: dict[str, tuple[Callable[[FinopsInput, FinopsReport], bool], str]] = {
    "INCOMPLETE_COMPARISON": (_any_incomplete, "비교에서 빠진 후보가 있습니다. 빠진 금액이나 항목을 채운 뒤 다시 비교하세요."),
    "NO_USAGE_PERIOD": (_no_usage_period, "사용량 근거 기간이 미제공입니다. 크기 제안을 적용하기 전에 사용량을 먼저 확인하세요."),
    "COST_INCREASE": (_any_increase, "비용이 늘어나는 후보가 있습니다. 비용 외 이전 사유를 승인 기록에 남기세요."),
    "NO_CONFIRMED_SAVING": (_no_confirmed_saving, "확실히 줄어드는 후보가 없으므로 비용 절감을 이전 근거로 쓰지 마세요."),
}


def enabled() -> bool:
    return os.environ.get("ALPHA_AI_ENABLED") == "1"


def enhance(inp: FinopsInput, report: FinopsReport) -> FinopsReport:
    if not enabled():
        return report
    try:
        notes = _ask_claude(_facts_for_prompt(inp, report))
        rendered = [] if notes is None else _render(notes, inp, report)
    except Exception as e:  # 어떤 실패든 규칙 결과로 돌아감
        log.warning("finops AI 보강 실패, 규칙 결과 사용 (%s)", type(e).__name__)
        return report
    return report.model_copy(update={"notes": [*report.notes, *rendered]})


def _render(notes: AiNotes, inp: FinopsInput, report: FinopsReport) -> list[str]:
    """고른 코드를 처음 나온 순서로 한 번씩 보고, 조건이 맞는 것만 문장으로 바꿉니다."""
    rendered = []
    for code in dict.fromkeys(notes.caveats):
        predicate, sentence = _CAVEATS[code]
        if predicate(inp, report):
            rendered.append(sentence)
    return rendered


def _facts_for_prompt(inp: FinopsInput, report: FinopsReport) -> str:
    # 허용 목록으로 만듭니다. 입력 문자열이 모델에 닿지 않고, 나중에 필드가 늘어도 새지 않습니다
    data = {
        "period_days": inp.period_days,
        "candidates": [
            {"monthly_saving_usd": s.monthly_saving_usd, "saving_pct": s.saving_pct, "complete": s.complete}
            for s in report.savings
        ],
        "has_best": report.best is not None,
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _ask_claude(facts: str) -> AiNotes | None:
    import anthropic  # 선택 의존성: pip install -e ".[ai]"

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM,
            messages=[{"role": "user", "content": facts}],
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
