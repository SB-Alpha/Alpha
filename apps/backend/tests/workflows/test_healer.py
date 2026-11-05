from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.features.workflows.healer import (
    SelfHealingDiagnosis,
    _rule_based_diagnosis,
    diagnose_run,
)
from app.features.workflows.schemas import (
    MigrationRequest,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)


def _sample_run(
    stage: StageName = StageName.PROVISIONING,
    state: WorkflowState = WorkflowState.FAILED,
    message: str = "terraform 바이너리를 찾을 수 없습니다",
) -> RunStatus:
    now = datetime.now(timezone.utc)
    return RunStatus(
        run_id="mig-test-healer-1",
        state=state,
        current_stage=stage,
        waiting_gate=None,
        stages=[
            StageStatus(
                stage=stage,
                state=state,
                progress=0,
                metrics={},
                message=message,
                manual=False,
                updated_at=now,
            )
        ],
        outputs={},
        approvals=[],
        request=MigrationRequest(
            github_repo_url="https://github.com/example/repo",
            aws_region="ap-northeast-2",
        ),
        created_at=now,
        updated_at=now,
        message=message,
    )


def test_rule_based_terraform_missing():
    diag = _rule_based_diagnosis(StageName.PROVISIONING, "terraform 바이너리를 찾을 수 없습니다")
    assert diag.category == "environment_dependency"
    assert "terraform" in diag.root_cause
    assert len(diag.action_plan) == 3
    assert diag.diagnosed_by == "rule"
    assert diag.suggested_manual_input is not None
    assert "target" in diag.suggested_manual_input


def test_rule_based_cdc_agent_url_missing():
    diag = _rule_based_diagnosis(StageName.CUTOVER, "CDC_AGENT_URL 미설정")
    assert diag.category == "network"
    assert diag.diagnosed_by == "rule"
    assert len(diag.action_plan) == 3


def test_diagnose_run_fallback_when_ai_disabled(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    run = _sample_run(message="terraform 바이너리를 찾을 수 없습니다")
    diag = diagnose_run(run)
    assert diag.category == "environment_dependency"
    assert diag.diagnosed_by == "rule"


def test_diagnose_run_ai_success(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")

    mock_diag = SelfHealingDiagnosis(
        category="environment_dependency",
        root_cause="Mock AI diagnosis root cause",
        action_plan=["Step 1", "Step 2", "Step 3"],
        suggested_manual_input={"target": {"project_id": "test"}},
        diagnosed_by="ai",
    )

    with patch("app.features.workflows.healer._ask_claude_healer", return_value=mock_diag):
        run = _sample_run()
        diag = diagnose_run(run)
        assert diag.diagnosed_by == "ai"
        assert diag.root_cause == "Mock AI diagnosis root cause"


def test_diagnose_run_fallback_on_ai_exception(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")

    with patch("app.features.workflows.healer._ask_claude_healer", side_effect=RuntimeError("API error")):
        run = _sample_run(message="terraform 바이너리를 찾을 수 없습니다")
        diag = diagnose_run(run)
        assert diag.diagnosed_by == "rule"
        assert diag.category == "environment_dependency"
