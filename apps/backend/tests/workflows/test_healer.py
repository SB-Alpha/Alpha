import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.features.db_migration.schemas import TargetInput
from app.features.workflows.healer import (
    AiDiagnosis,
    SelfHealingDiagnosis,
    _rule_based_diagnosis,
    diagnose_run,
)
from app.features.workflows.router import get_diagnosis
from app.features.workflows.schemas import (
    MigrationRequest,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)

TERRAFORM_MISSING = "terraform 실행 파일을 찾을 수 없습니다: terraform"


def _sample_run(
    stage: StageName = StageName.PROVISIONING,
    state: WorkflowState = WorkflowState.FAILED,
    message: str = TERRAFORM_MISSING,
) -> RunStatus:
    now = datetime.now(timezone.utc)
    return RunStatus(
        run_id="mig-test-healer-1",
        state=state,
        current_stage=stage,
        waiting_gate=None,
        stages=[
            StageStatus(stage=StageName.PLANNING, state=WorkflowState.SUCCEEDED, progress=100,
                        metrics={}, message="이전 실패 없이 완료", manual=False, updated_at=now),
            StageStatus(stage=stage, state=state, progress=0, metrics={}, message=message,
                        manual=False, updated_at=now),
        ],
        outputs={},
        approvals=[],
        request=MigrationRequest(
            github_repo_url="https://github.com/example/repo",
            aws_region="ap-northeast-2",
            aws_credentials_ref="secret://mig/source-cloud",
        ),
        created_at=now,
        updated_at=now,
        message=message,
    )


@pytest.fixture
def no_simulation(monkeypatch):
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)


@pytest.fixture
def simulation(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")


def test_terraform_missing_gives_no_invented_input_outside_simulation(no_simulation):
    diag = _rule_based_diagnosis(StageName.PROVISIONING, TERRAFORM_MISSING)
    assert diag.category == "environment_dependency"
    assert diag.diagnosed_by == "rule"
    assert len(diag.action_plan) == 3
    assert diag.suggested_manual_input is None


def test_real_agent_url_gives_no_invented_input_even_with_simulation_flag(monkeypatch):
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    assert _rule_based_diagnosis(StageName.PROVISIONING, TERRAFORM_MISSING).suggested_manual_input is None


def test_simulation_input_passes_db_migration_validation_and_is_marked(simulation):
    fixture = _rule_based_diagnosis(StageName.PROVISIONING, TERRAFORM_MISSING).suggested_manual_input
    assert fixture["simulated"] is True
    TargetInput.model_validate(fixture["target"])


def test_simulation_input_for_app_deploy_is_not_marked_healthy(simulation):
    fixture = _rule_based_diagnosis(StageName.APP_DEPLOY, TERRAFORM_MISSING).suggested_manual_input
    assert fixture["simulated"] is True
    assert fixture["healthy"] is False


def test_simulation_input_is_not_offered_for_other_stages(simulation):
    assert _rule_based_diagnosis(StageName.CUTOVER, TERRAFORM_MISSING).suggested_manual_input is None


def test_rule_based_cdc_agent_url_missing():
    diag = _rule_based_diagnosis(StageName.CUTOVER, "CDC_AGENT_URL 미설정")
    assert diag.category == "network"
    assert diag.diagnosed_by == "rule"
    assert len(diag.action_plan) == 3


def test_diagnose_run_uses_rules_when_ai_disabled(monkeypatch, no_simulation):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    diag = diagnose_run(_sample_run())
    assert diag.category == "environment_dependency"
    assert diag.diagnosed_by == "rule"


def test_diagnose_run_picks_the_failed_stage(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    run = _sample_run(stage=StageName.DB_MIGRATION, message="boom")
    assert "db_migration" in diagnose_run(run).root_cause


def test_diagnose_run_falls_back_on_ai_exception(monkeypatch, no_simulation):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    with patch("app.features.workflows.healer._ask_claude_healer", side_effect=RuntimeError("API error")):
        diag = diagnose_run(_sample_run())
    assert diag.diagnosed_by == "rule"
    assert diag.category == "environment_dependency"


def test_ai_output_schema_has_no_free_form_object():
    assert "suggested_manual_input" not in AiDiagnosis.model_json_schema()["properties"]


def test_ai_call_is_parsed_and_payload_has_no_request_or_outputs(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(parsed_output=AiDiagnosis(category="unknown", root_cause="원인", action_plan=["a", "b", "c"]))

    fake = ModuleType("anthropic")
    fake.Anthropic = lambda **_: SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    diag = diagnose_run(_sample_run())

    assert diag.diagnosed_by == "ai"
    assert diag.suggested_manual_input is None
    assert seen["output_format"] is AiDiagnosis
    content = seen["messages"][0]["content"]
    assert "secret://" not in content
    assert json.loads(content)["failed_stage"] == "provisioning"


def test_ai_call_payload_scrubs_credentials_and_urls(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(parsed_output=AiDiagnosis(category="unknown", root_cause="원인", action_plan=["a", "b", "c"]))

    fake = ModuleType("anthropic")
    fake.Anthropic = lambda **_: SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    run = _sample_run(message="connection error postgresql://admin:secretpass@10.0.1.5:5432/mydb")
    diag = diagnose_run(run)

    assert diag.diagnosed_by == "ai"
    content = json.loads(seen["messages"][0]["content"])
    assert "secretpass" not in content["error_message"]
    assert "10.0.1.5" not in content["error_message"]
    assert "[숨김]" in content["error_message"]


def test_diagnosis_endpoint_returns_report_for_failed_run(no_simulation, monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    engine = SimpleNamespace(get=lambda run_id: _sample_run())
    assert isinstance(get_diagnosis("mig-test-healer-1", engine), SelfHealingDiagnosis)


def test_diagnosis_endpoint_rejects_paused_run():
    run = _sample_run(state=WorkflowState.PAUSED, message="미구현 — 수동 입력 대기")
    engine = SimpleNamespace(get=lambda run_id: run)
    with pytest.raises(HTTPException) as exc:
        get_diagnosis("mig-test-healer-1", engine)
    assert exc.value.status_code == 409
