"""AI 보강은 Claude 를 실제로 부르지 않고 _ask_claude 를 바꿔 끼워 확인합니다."""

from app.features.assessment import ai
from app.features.assessment.ai import AiNotes, AiRiskNote

from .test_plan import _ready_input, run

NOTES = AiNotes(
    summary=["target-app 을 서울 리전으로 옮깁니다.", "필수 조건 4개 모두 통과.", "승인하면 목적지를 만듭니다."],
    risk_notes=[AiRiskNote(level="low", title="첫 요청 지연", detail="Cloud Run 최소 인스턴스가 0 입니다.")],
)


def test_disabled_by_default_returns_rule_plan(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    plan = run(_ready_input())
    assert ai.enhance(plan) is plan


def test_ai_only_changes_text_not_decision(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setattr(ai, "_ask_claude", lambda plan: NOTES)
    plan = run(_ready_input(logical_replication=None, wal_level=None))  # 승인 불가 계획
    out = ai.enhance(plan)
    assert out.summary == NOTES.summary and out.summary_source == "ai"
    assert out.approvable is False and out.blocking == plan.blocking
    assert out.checks == plan.checks and out.target_infra == plan.target_infra
    assert out.risks[-1].source == "ai" and len(out.risks) == len(plan.risks) + 1


def test_ai_failure_or_bad_shape_falls_back(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    plan = run(_ready_input())

    def boom(plan):
        raise RuntimeError("network")

    monkeypatch.setattr(ai, "_ask_claude", boom)
    assert ai.enhance(plan) is plan

    monkeypatch.setattr(ai, "_ask_claude", lambda p: AiNotes(summary=["한 줄만"]))
    assert ai.enhance(plan) is plan

    monkeypatch.setattr(ai, "_ask_claude", lambda p: None)
    assert ai.enhance(plan) is plan


def test_prompt_sends_env_names_not_values():
    plan = run(_ready_input())
    text = ai._plan_for_prompt(plan)
    assert "LOG_LEVEL" in text and '"INFO"' not in text
