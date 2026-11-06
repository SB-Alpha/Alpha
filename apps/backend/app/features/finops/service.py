"""finops 진입 함수. 비용 계산 결과를 받아 절감을 계산하고 설명 문장을 붙입니다. (이주호)

흐름: FinopsInput → 절감 계산(코드) → 규칙 문장 → (켜져 있으면) AI 문장으로 교체 → FinopsReport
AI 는 숫자를 바꾸지 않습니다. savings · best 는 항상 코드가 낸 값입니다.
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
        report(80, "AI 로 제안 문장 다듬기")
        out = ai_part.enhance(inp, out)  # 실패하거나 없는 숫자를 쓰면 규칙 문장 그대로
    report(100, "비용 비교 완료")
    return out


def total(cost: CloudCost) -> tuple[float | None, bool]:
    """(월 합계, 모든 항목에 금액이 있는지). 금액이 하나도 없으면 합계는 None."""
    known = [line.monthly_usd for line in cost.lines if line.monthly_usd is not None]
    complete = bool(cost.lines) and len(known) == len(cost.lines)
    return (round(sum(known), 2) if known else None), complete


def saving(current: CloudCost, candidate: CloudCost) -> Saving:
    cur, cur_complete = total(current)
    cand, cand_complete = total(candidate)
    complete = cur_complete and cand_complete
    # 일부 항목만 더한 합계로 절감을 내면 실제보다 싸 보이므로, 양쪽이 다 채워졌을 때만 계산합니다
    diff = round(cur - cand, 2) if complete and cur is not None and cand is not None else None
    pct = round(diff / cur * 100, 1) if diff is not None and cur else None
    return Saving(cloud=candidate.cloud, current_monthly_usd=cur, candidate_monthly_usd=cand,
                  monthly_saving_usd=diff, saving_pct=pct, complete=complete)


def _best(savings: list[Saving]) -> str | None:
    full = [s for s in savings if s.complete and s.monthly_saving_usd is not None and s.monthly_saving_usd > 0]
    return max(full, key=lambda s: s.monthly_saving_usd).cloud if full else None


def _usd(v: float | None) -> str:
    return "미제공" if v is None else f"${v:,.2f}"


def rule_summary(inp: FinopsInput, savings: list[Saving], best: str | None) -> list[str]:
    cur, _ = total(inp.current)
    lines = [f"현재 {inp.current.cloud} 월 비용은 {_usd(cur)} 입니다."]
    for s in savings:
        if s.monthly_saving_usd is None:
            known = f"(아는 항목 합계 {_usd(s.candidate_monthly_usd)})" if s.candidate_monthly_usd is not None else ""
            lines.append(f"{s.cloud} 는 금액이 빠진 항목이 있어 아직 비교할 수 없습니다{known}.")
        elif s.monthly_saving_usd >= 0:
            pct = f" ({s.saving_pct}%)" if s.saving_pct is not None else ""
            lines.append(f"{s.cloud} 로 옮기면 월 {_usd(s.monthly_saving_usd)}{pct} 줄어들 것으로 계산됩니다.")
        else:
            lines.append(f"{s.cloud} 로 옮기면 월 {_usd(-s.monthly_saving_usd)} 늘어날 것으로 계산됩니다.")
    lines.append(f"가장 많이 아끼는 후보는 {best} 입니다." if best else "확실히 아끼는 후보는 아직 없습니다.")
    return lines


def rule_notes(inp: FinopsInput, savings: list[Saving]) -> list[str]:
    notes = [f"{s.cloud} 비교는 일부 항목 금액이 빠진 합계입니다." for s in savings if not s.complete]
    if inp.period_days is None:
        notes.append("사용량을 본 기간이 미제공이라 크기 제안의 근거가 약할 수 있습니다.")
    notes += [f"{r.resource}: {r.current} → {r.suggested} 제안 ({r.evidence})" for r in inp.rightsizing]
    return notes
