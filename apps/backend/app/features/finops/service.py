"""finops 진입 함수. 비용 계산 결과를 받아 절감을 계산하고 설명 문장을 붙입니다. (이주호)

흐름: FinopsInput → 절감 계산(코드) → 규칙 문장·주의점 → (켜져 있으면) AI 설명을 주의점 뒤에 덧붙임 → FinopsReport
금액·증감·추천 문장과 savings · best 는 항상 코드가 냅니다. AI 는 숫자 없는 설명만 씁니다.
"""

from __future__ import annotations

from typing import Any, Protocol

from . import ai as ai_part
from .schemas import CloudCost, FinopsInput, FinopsReport, Saving


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
        report(80, "AI 설명 덧붙이기")
        out = ai_part.enhance(inp, out)  # 실패하거나 숫자·추천을 쓰면 규칙 결과 그대로
    report(100, "비용 비교 완료")
    return out


def total(cost: CloudCost) -> tuple[float | None, bool]:
    """(아는 항목 합계, 모든 항목에 금액이 있는지). 금액이 하나도 없으면 합계는 None."""
    known = [line.monthly_usd for line in cost.lines if line.monthly_usd is not None]
    complete = bool(cost.lines) and len(known) == len(cost.lines)
    return (round(sum(known), 2) if known else None), complete


def complete_total(cost: CloudCost) -> float | None:
    """모든 항목에 금액이 있을 때만 합계. 일부만 더한 값은 실제보다 싸 보이므로 돌려주지 않습니다."""
    known, complete = total(cost)
    return known if complete else None


def _unpriced(cost: CloudCost) -> list[str]:
    return [line.item for line in cost.lines if line.monthly_usd is None]


def missing_items(current: CloudCost, candidate: CloudCost) -> list[str]:
    """금액이 없는 항목과, 원본에는 있는데 후보에 통째로 빠진 항목."""
    candidate_items = {line.item.strip() for line in candidate.lines}
    absent = [line.item for line in current.lines if line.item.strip() not in candidate_items]
    found = [f"{current.cloud} {item}" for item in _unpriced(current)]
    found += [f"{candidate.cloud} {item}" for item in _unpriced(candidate) + absent]
    return list(dict.fromkeys(found))


def saving(current: CloudCost, candidate: CloudCost) -> Saving:
    cur = complete_total(current)
    cand, _ = total(candidate)
    missing = missing_items(current, candidate)
    # 후보에 원본 항목이 빠져 있으면 금액이 다 있어도 싸 보이는 견적이므로 완전한 비교로 치지 않습니다
    complete = cur is not None and cand is not None and bool(candidate.lines) and not missing
    diff = round(cur - cand, 2) if complete else None
    pct = round(diff / cur * 100, 1) if diff is not None and cur else None
    return Saving(cloud=candidate.cloud, current_monthly_usd=cur, candidate_monthly_usd=cand,
                  monthly_saving_usd=diff, saving_pct=pct, complete=complete, missing=missing)


def _best(savings: list[Saving]) -> str | None:
    full = [s for s in savings if s.complete and s.monthly_saving_usd is not None and s.monthly_saving_usd > 0]
    return max(full, key=lambda s: s.monthly_saving_usd).cloud if full else None


def _usd(v: float | None) -> str:
    return "미제공" if v is None else f"${v:,.2f}"


def rule_summary(inp: FinopsInput, savings: list[Saving], best: str | None) -> list[str]:
    known, complete = total(inp.current)
    if complete:
        lines = [f"현재 {inp.current.cloud} 월 비용은 {_usd(known)} 입니다."]
    else:
        part = f" (아는 항목 합계 {_usd(known)})" if known is not None else ""
        lines = [f"현재 {inp.current.cloud} 월 비용은 금액이 빠진 항목이 있어 미제공입니다{part}."]
    for s in savings:
        if s.monthly_saving_usd is None:
            known_c = f" (아는 항목 합계 {_usd(s.candidate_monthly_usd)})" if s.candidate_monthly_usd is not None else ""
            lines.append(f"{s.cloud} 는 금액이 빠진 항목이 있어 아직 비교할 수 없습니다{known_c}.")
        elif s.monthly_saving_usd >= 0:
            pct = f" ({s.saving_pct}%)" if s.saving_pct is not None else ""
            lines.append(f"{s.cloud} 로 옮기면 월 {_usd(s.monthly_saving_usd)}{pct} 줄어들 것으로 계산됩니다.")
        else:
            lines.append(f"{s.cloud} 로 옮기면 월 {_usd(-s.monthly_saving_usd)} 늘어날 것으로 계산됩니다.")
    lines.append(f"가장 많이 아끼는 후보는 {best} 입니다." if best else "확실히 아끼는 후보는 아직 없습니다.")
    return lines


def rule_notes(inp: FinopsInput, savings: list[Saving]) -> list[str]:
    notes = [f"{s.cloud} 비교는 일부 항목 금액이 빠졌습니다: {', '.join(s.missing)}" for s in savings if s.missing]
    if inp.period_days is None and inp.rightsizing:  # 크기 제안이 있을 때만 근거 기간이 의미 있음
        notes.append("사용량을 본 기간이 미제공이라 크기 제안의 근거가 약할 수 있습니다.")
    notes += [f"{r.resource}: {r.current} → {r.suggested} 제안 ({r.evidence})" for r in inp.rightsizing]
    notes += inp.assumptions
    return notes
