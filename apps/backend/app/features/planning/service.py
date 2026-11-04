"""planning 진입 함수. workflows 가 run() 을 호출합니다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from . import rules
from .schemas import MigrationPlan, PlanningInput


class Reporter(Protocol):
    """RunContext 가 확정되기 전까지 쓰는 최소 형태 (workflows 의 RunContext 로 교체)."""

    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None,
               message: str | None = None) -> None: ...


def run(inp: PlanningInput, ctx: Reporter | None = None) -> MigrationPlan:
    def report(progress: int, message: str, state: str = "running") -> None:
        if ctx is not None:
            ctx.report(state, progress, None, message)

    report(10, "필수 조건 검사")
    checks = rules.run_checks(inp.inventory)

    report(50, "목적지 구성 매핑")
    infra, mapping_risks = rules.build_target_infra(inp.inventory, inp.target)

    risks = rules.collect_risks(inp.inventory, checks) + mapping_risks
    blocking = [f"{c.title}: {c.detail}" for c in checks if c.result != "pass"]
    approvable = not blocking

    plan = MigrationPlan(
        target_infra=infra,
        checks=checks,
        risks=risks,
        summary=rules.rule_summary(infra, checks, approvable),
        summary_source="rule",
        steps=rules.build_steps(),
        approvable=approvable,
        blocking=blocking,
        generated_at=datetime.now(timezone.utc),
    )

    # AI 위험 설명·요약은 모델이 정해지면 여기서 plan.risks / plan.summary 를 보강합니다.
    # AI 호출이 실패해도 위의 규칙 기반 plan 은 그대로 반환합니다.

    report(100, "계획 생성 완료", state="waiting_approval")
    return plan
