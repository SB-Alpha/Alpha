"""finops 진입 함수. 비용 계산 결과를 받아 절감을 계산하고 설명 문장을 붙입니다. (이주호)

흐름: FinopsInput → 절감 계산(코드) → 규칙 문장 → (켜져 있으면) AI 가 고른 주의점 추가 → FinopsReport
AI 는 정해진 주의점 코드만 고르고, 규칙 문장은 그대로 남습니다. savings · best 는 항상 코드가 낸 값입니다.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Literal, Protocol

from . import ai as ai_part
from .schemas import CloudCost, FinopsInput, FinopsReport, Saving

Reason = Literal["items", "current_amount", "candidate_amount"]


class Reporter(Protocol):
    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None,
               message: str | None = None) -> None: ...


def run(inp: FinopsInput, ctx: Reporter | None = None) -> FinopsReport:
    def report(progress: int, message: str) -> None:
        if ctx is not None:
            ctx.report("running", progress, None, message)

    report(20, "절감 계산")
    savings = [saving(inp.current, c) for c in inp.candidates]
    best = _best(savings)
    report(60, "제안 문장 만들기")
    out = FinopsReport(savings=savings, best=best, summary=rule_summary(inp, savings, best),
                       notes=rule_notes(inp, savings))
    if ai_part.enabled():
        report(80, "AI 로 주의점 고르기")
        out = ai_part.enhance(inp, out)  # AI 는 주의점 코드만 고릅니다. 실패하면 규칙 결과 그대로
    report(100, "비용 비교 완료")
    return out


def total(cost: CloudCost) -> tuple[float | None, bool]:
    """(월 합계, 모든 항목에 금액이 있는지). 금액이 하나도 없거나 합계가 넘치면 합계는 None."""
    known = [line.monthly_usd for line in cost.lines if line.monthly_usd is not None]
    amount = sum(known)
    if not math.isfinite(amount):  # 항목마다 유한해도 더하면 넘칠 수 있습니다
        return None, False
    complete = bool(cost.lines) and len(known) == len(cost.lines)
    return (round(amount, 2) if known else None), complete


def _item_key(item: str) -> str:
    return item.strip().casefold()


def _incomparable_reason(current: CloudCost, candidate: CloudCost) -> Reason | None:
    """두 합계를 비교할 수 없는 이유. 항목 구성이 다르면 금액이 다 있어도 합계 비교는 의미가 없습니다."""
    if Counter(_item_key(x.item) for x in current.lines) != Counter(_item_key(x.item) for x in candidate.lines):
        return "items"
    if not total(current)[1]:
        return "current_amount"
    if not total(candidate)[1]:
        return "candidate_amount"
    return None


def saving(current: CloudCost, candidate: CloudCost) -> Saving:
    cur, _ = total(current)
    cand, _ = total(candidate)
    complete = _incomparable_reason(current, candidate) is None
    # 일부 항목만 더한 합계로 절감을 내면 실제보다 싸 보이므로, 양쪽이 다 채워졌을 때만 계산합니다
    diff = round(cur - cand, 2) if complete and cur is not None and cand is not None else None
    return Saving(cloud=candidate.cloud, current_monthly_usd=cur, candidate_monthly_usd=cand,
                  monthly_saving_usd=diff, saving_pct=_percent(diff, cur), complete=complete)


def _percent(diff: float | None, cur: float | None) -> float | None:
    if diff is None or not cur:
        return None
    pct = diff / cur * 100
    return round(pct, 1) if math.isfinite(pct) else None  # 현재 비용이 아주 작으면 계산이 넘칠 수 있습니다


def _top_savers(savings: list[Saving]) -> list[Saving]:
    """비교 가능한 후보 중 절감액이 가장 큰 후보들. 절감이 없으면 빈 목록."""
    full = [s for s in savings if s.complete and s.monthly_saving_usd is not None and s.monthly_saving_usd > 0]
    top = max((s.monthly_saving_usd for s in full), default=None)
    return [s for s in full if s.monthly_saving_usd == top]


def _best(savings: list[Saving]) -> str | None:
    top = _top_savers(savings)
    return top[0].cloud if len(top) == 1 else None  # 동률이면 코드가 임의로 고르지 않습니다


def _usd(v: float | None) -> str:
    return "미제공" if v is None else f"${v:,.2f}"


def _current_sentence(current: CloudCost) -> str:
    cur, complete = total(current)
    if cur is not None and not complete:
        return f"현재 {current.cloud} 월 비용은 아는 항목 합계 {_usd(cur)} 이고, 금액이 빠진 항목이 있습니다."
    return f"현재 {current.cloud} 월 비용은 {_usd(cur)} 입니다."


def _incomparable_sentence(s: Saving, reason: Reason) -> str:
    if reason == "items":
        return f"{s.cloud} 는 비교 항목이 현재와 달라 아직 비교할 수 없습니다."
    if reason == "current_amount":
        return f"{s.cloud} 는 현재 비용에 금액이 빠진 항목이 있어 아직 비교할 수 없습니다."
    known = f"(아는 항목 합계 {_usd(s.candidate_monthly_usd)})" if s.candidate_monthly_usd is not None else ""
    return f"{s.cloud} 는 금액이 빠진 항목이 있어 아직 비교할 수 없습니다{known}."


def _candidate_sentence(s: Saving, reason: Reason | None) -> str:
    if reason is not None:
        return _incomparable_sentence(s, reason)
    if s.monthly_saving_usd > 0:
        pct = f" ({s.saving_pct}%)" if s.saving_pct is not None else ""
        return f"{s.cloud} 로 옮기면 월 {_usd(s.monthly_saving_usd)}{pct} 줄어들 것으로 계산됩니다."
    if s.monthly_saving_usd == 0:
        return f"{s.cloud} 로 옮겨도 월 비용 차이가 없습니다."
    pct = f" ({abs(s.saving_pct)}%)" if s.saving_pct is not None else ""
    return f"{s.cloud} 로 옮기면 월 {_usd(-s.monthly_saving_usd)}{pct} 늘어날 것으로 계산됩니다."


def _conclusion_sentence(savings: list[Saving], best: str | None) -> str:
    if best:
        return f"가장 많이 아끼는 후보는 {best} 입니다."
    top = _top_savers(savings)
    if len(top) > 1:
        return f"{', '.join(s.cloud for s in top)} 의 절감액이 같아 하나를 고르지 않았습니다."
    return "확실히 아끼는 후보는 아직 없습니다."


def rule_summary(inp: FinopsInput, savings: list[Saving], best: str | None) -> list[str]:
    lines = [_current_sentence(inp.current)]
    for candidate, s in zip(inp.candidates, savings, strict=True):
        lines.append(_candidate_sentence(s, _incomparable_reason(inp.current, candidate)))
    lines.append(_conclusion_sentence(savings, best))
    return lines


def _only_in(cost: CloudCost, other: CloudCost) -> str:
    """cost 에만 있는 항목 이름. 같은 이름이 더 많은 만큼도 '에만 있는' 항목으로 셉니다."""
    remaining = Counter(_item_key(line.item) for line in other.lines)
    names = []
    for line in cost.lines:
        key = _item_key(line.item)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            names.append(line.item.strip())
    return ", ".join(names) or "없음"


def _comparison_note(current: CloudCost, candidate: CloudCost, cloud: str) -> str | None:
    reason = _incomparable_reason(current, candidate)
    if reason == "items":
        only_current, only_candidate = _only_in(current, candidate), _only_in(candidate, current)
        return f"{cloud} 비교 항목이 다릅니다 (현재에만: {only_current}; 후보에만: {only_candidate})."
    return f"{cloud} 비교는 일부 항목 금액이 빠진 합계입니다." if reason else None


def rule_notes(inp: FinopsInput, savings: list[Saving]) -> list[str]:
    compared = [_comparison_note(inp.current, c, s.cloud) for c, s in zip(inp.candidates, savings, strict=True)]
    notes = [n for n in compared if n is not None]
    if inp.period_days is None:
        notes.append("사용량을 본 기간이 미제공이라 크기 제안의 근거가 약할 수 있습니다.")
    # evidence 는 입력 문장이라 그대로 옮기지 않습니다 (숫자·추천이 코드 결과처럼 보일 수 있음)
    notes += [f"{r.resource}: {r.current} → {r.suggested} 제안 (근거 문장은 입력값이며 코드가 확인하지 않았습니다)"
              for r in inp.rightsizing]
    return notes
