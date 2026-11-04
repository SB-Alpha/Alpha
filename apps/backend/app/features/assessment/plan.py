"""원본 사실(InventoryForPlanning) → 승인할 수 있는 MigrationPlan. (이주호)

판정과 매핑은 전부 rules.py 의 코드로 합니다. AI 판단으로 통과시키지 않습니다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import rules
from .schemas import MigrationPlan, PlanningInput


def build(inp: PlanningInput) -> MigrationPlan:
    checks = rules.run_checks(inp.inventory)
    infra, mapping_risks = rules.build_target_infra(inp.inventory, inp.target)

    risks = rules.collect_risks(inp.inventory, checks) + mapping_risks
    blocking = [f"{c.title}: {c.detail}" for c in checks if c.result != "pass"]
    approvable = not blocking

    # AI 위험 설명·요약은 모델이 정해지면 여기서 risks / summary 를 보강합니다.
    # AI 호출이 실패해도 아래의 규칙 기반 plan 은 그대로 반환합니다.
    return MigrationPlan(
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
