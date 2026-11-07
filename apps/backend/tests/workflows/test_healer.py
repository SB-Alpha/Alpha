import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

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
def no_agent(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)


def test_terraform_missing_gives_no_invented_input(no_agent):
    diag = _rule_based_diagnosis(StageName.PROVISIONING, TERRAFORM_MISSING)
    assert diag.category == "environment_dependency"
    assert diag.diagnosed_by == "rule"
    assert len(diag.action_plan) == 3
    assert diag.suggested_manual_input is None


@pytest.mark.parametrize("stage", [StageName.PROVISIONING, StageName.APP_DEPLOY, StageName.CUTOVER])
@pytest.mark.parametrize("agent_url", [None, "https://cdc.example", "mock://agent"])
def test_rules_never_invent_manual_input(monkeypatch, stage, agent_url):
    # 실제 인프라가 아닌 값을 수동 입력 추천으로 주지 않습니다. 에이전트 주소가 무엇이든 같습니다.
    if agent_url is None:
        monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    else:
        monkeypatch.setenv("CDC_AGENT_URL", agent_url)
    assert _rule_based_diagnosis(stage, TERRAFORM_MISSING).suggested_manual_input is None


def test_rule_based_cdc_agent_url_missing():
    diag = _rule_based_diagnosis(StageName.CUTOVER, "CDC_AGENT_URL 미설정")
    assert diag.category == "network"
    assert diag.diagnosed_by == "rule"
    assert len(diag.action_plan) == 3


def test_rule_based_cdc_agent_url_points_to_the_real_agent_status():
    plan = " ".join(_rule_based_diagnosis(StageName.CUTOVER, "CDC_AGENT_URL 미설정").action_plan)
    assert "/status" in plan
    assert "시뮬레이션" not in plan and "ALPHA_CDC_SIMULATION" not in plan


def test_diagnose_run_uses_rules_when_ai_disabled(monkeypatch, no_agent):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    diag = diagnose_run(_sample_run())
    assert diag.category == "environment_dependency"
    assert diag.diagnosed_by == "rule"


def test_diagnose_run_picks_the_failed_stage(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    run = _sample_run(stage=StageName.DB_MIGRATION, message="boom")
    assert "db_migration" in diagnose_run(run).root_cause


def test_diagnose_run_falls_back_on_ai_exception(monkeypatch, no_agent):
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


def test_diagnosis_endpoint_returns_report_for_failed_run(no_agent, monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)
    engine = SimpleNamespace(get=lambda run_id: _sample_run())
    assert isinstance(get_diagnosis("mig-test-healer-1", engine), SelfHealingDiagnosis)


def test_diagnosis_endpoint_rejects_paused_run():
    run = _sample_run(state=WorkflowState.PAUSED, message="미구현 — 수동 입력 대기")
    engine = SimpleNamespace(get=lambda run_id: run)
    with pytest.raises(HTTPException) as exc:
        get_diagnosis("mig-test-healer-1", engine)
    assert exc.value.status_code == 409
