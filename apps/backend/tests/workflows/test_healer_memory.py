"""ReasoningBank (#77) 가 자가치유 진단과 엔진에 붙는 방식."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from app.features.workflows import healer
from app.features.workflows.engine import Engine
from app.features.workflows.healer import AiDiagnosis, diagnose_run
from app.features.workflows.reasoning_bank import ReasoningBank, fingerprint
from app.features.workflows.router import get_diagnosis, rediagnose
from app.features.workflows.schemas import (
    ManualStageInput,
    MigrationRequest,
    RunContext,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from app.features.workflows.store import RunStore

ERROR = "TimeoutError: VPN 터널 10.0.0.5 응답 없음 (run mig-20261107-aaaaaaaa)"
REQUEST = MigrationRequest(github_repo_url="https://github.com/example/repo", aws_region="ap-northeast-2")


def failed_run(message: str = ERROR, run_id: str = "mig-test-1", stage: StageName = StageName.PROVISIONING) -> RunStatus:
    now = datetime.now(timezone.utc)
    return RunStatus(
        run_id=run_id,
        state=WorkflowState.FAILED,
        current_stage=stage,
        stages=[
            StageStatus(stage=stage, state=WorkflowState.FAILED, progress=0, metrics={}, message=message,
                        manual=False, updated_at=now),
        ],
        request=REQUEST,
        created_at=now,
        updated_at=now,
        message=message,
    )


@pytest.fixture
def bank(tmp_path):
    return ReasoningBank(tmp_path / "bank.sqlite3")


@pytest.fixture(autouse=True)
def no_simulation(monkeypatch):
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)


def teach(bank, error=ERROR, run_id="mig-old", stage="provisioning", **over):
    fields = dict(category="network", root_cause="VPN 터널이 아직 올라오지 않았습니다.",
                  action_plan=["터널 상태를 확인합니다.", "라우트를 확인합니다.", "재시도합니다."])
    fields.update(over)
    bank.open_incident(run_id, stage, "gcp", error, from_memory=False, **fields)
    assert bank.resolve(run_id, stage, "retry")


def fake_anthropic(monkeypatch, response):
    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        return response

    fake = ModuleType("anthropic")
    fake.Anthropic = lambda **_: SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return seen


def ai_answer(**over):
    fields = dict(category="network", root_cause="AI 원인", action_plan=["a", "b", "c"])
    fields.update(over)
    return SimpleNamespace(parsed_output=AiDiagnosis(**fields), stop_reason=over.pop("stop_reason", "end_turn"))


# -- Case 1: 기억이 믿을 만하면 LLM 없이 -------------------------------------


def test_a_remembered_fix_is_returned_without_calling_the_llm(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    teach(bank)
    with patch.object(healer, "_ask_claude_healer", side_effect=AssertionError("LLM 을 부르면 안 됨")):
        diag = diagnose_run(failed_run(run_id="mig-new"), bank=bank)
    assert diag.diagnosed_by == "reasoning_bank"
    assert diag.memory_hit is True and diag.prior_success_rate == 1.0
    assert diag.category == "network" and len(diag.action_plan) == 3


def test_memory_never_supplies_manual_input(bank):
    teach(bank)
    assert diagnose_run(failed_run(run_id="mig-new"), bank=bank).suggested_manual_input is None


def test_reasoning_bank_can_be_bypassed_for_a_fresh_diagnosis(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    teach(bank)
    fake_anthropic(monkeypatch, ai_answer())
    diag = diagnose_run(failed_run(run_id="mig-new"), use_memory=False, bank=bank)
    assert diag.diagnosed_by == "ai" and diag.memory_hit is False and diag.prior_success_rate is None


def test_a_memory_with_a_poor_record_is_not_followed(bank):
    teach(bank)
    for i in range(2):
        bank.open_incident(f"mig-f{i}", "provisioning", "gcp", ERROR, category="network", root_cause="x",
                           action_plan=["a"], from_memory=True)
        bank.fail(f"mig-f{i}", "provisioning")
    diag = diagnose_run(failed_run(run_id="mig-new"), bank=bank)
    assert diag.diagnosed_by == "rule" and diag.memory_hit is False


def test_a_different_error_is_not_answered_from_memory(bank):
    teach(bank)
    diag = diagnose_run(failed_run("PermissionError: 403 forbidden", run_id="mig-new"), bank=bank)
    assert diag.diagnosed_by == "rule"


# -- Case 2: 새 장애는 참고 데이터와 함께 AI 에게 ----------------------------


def test_new_incident_gets_past_cases_as_data_and_a_roomy_token_limit(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    teach(bank, error="CDC_AGENT_URL 미설정", run_id="mig-1", category="network", root_cause="예전 사례 원인")
    seen = fake_anthropic(monkeypatch, ai_answer())
    diag = diagnose_run(failed_run(run_id="mig-new"), bank=bank)
    assert diag.diagnosed_by == "ai"
    assert seen["max_tokens"] == healer.MAX_TOKENS >= 4000
    payload = json.loads(seen["messages"][0]["content"])
    assert payload["past_cases"][0]["root_cause"] == "예전 사례 원인"
    assert payload["past_cases"][0]["resolved_count"] == 1
    assert "데이터" in seen["system"] and "past_cases" in seen["system"]


def test_without_past_cases_the_payload_has_no_such_key(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    seen = fake_anthropic(monkeypatch, ai_answer())
    diagnose_run(failed_run(), bank=bank)
    assert "past_cases" not in json.loads(seen["messages"][0]["content"])


# -- AI 응답 처리 -----------------------------------------------------------


def test_a_response_cut_off_by_the_token_limit_falls_back_to_the_rules(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    fake_anthropic(monkeypatch, SimpleNamespace(parsed_output=AiDiagnosis(category="network", root_cause="잘림",
                                                                         action_plan=["a", "b", "c"]),
                                                stop_reason="max_tokens"))
    diag = diagnose_run(failed_run("terraform 실행 파일을 찾을 수 없습니다: terraform"), bank=bank)
    assert diag.diagnosed_by == "rule"
    assert diag.root_cause != "잘림"


def test_ai_category_and_plan_are_cleaned(monkeypatch, bank):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    fake_anthropic(monkeypatch, ai_answer(category="완전 새로운 분류", action_plan=["1", " ", "2", "3", "4"]))
    diag = diagnose_run(failed_run(), bank=bank)
    assert diag.category == "unknown"
    assert diag.action_plan == ["1", "2", "3"]


# -- 저장·장애 격리 ---------------------------------------------------------


def test_diagnosis_still_works_when_the_bank_is_broken(monkeypatch):
    class Broken:
        def lookup(self, *a, **k):
            raise RuntimeError("disk full")

        similar = open_incident = lookup

    diag = diagnose_run(failed_run(), bank=Broken())
    assert diag.diagnosed_by == "rule"


def test_diagnosis_works_without_a_bank(monkeypatch):
    monkeypatch.setenv("ALPHA_REASONING_BANK", "0")
    assert diagnose_run(failed_run()).diagnosed_by == "rule"


def test_diagnosed_text_is_scrubbed_before_it_is_kept(bank):
    secret_error = "connection error postgresql://admin:secretpass@10.0.1.5:5432/mydb"
    run = failed_run(secret_error, run_id="mig-s1")
    diagnose_run(run, bank=bank)  # unknown 진단의 root_cause 에는 에러 문장이 들어간다
    assert bank.resolve("mig-s1", "provisioning", "retry")
    episode = bank.lookup("provisioning", "gcp", secret_error)
    stored = json.dumps(episode.model_dump(), ensure_ascii=False)
    assert "secretpass" not in stored and "10.0.1.5" not in stored


def test_endpoints_split_memory_get_and_fresh_post(monkeypatch, tmp_path):
    engine = Engine(RunStore(tmp_path / "w.sqlite3"), {})
    run = engine.create_run(REQUEST)
    engine._mark_failed(run.run_id, StageName.PROVISIONING, ERROR)
    bank = ReasoningBank(tmp_path / "reasoning_bank.sqlite3")  # conftest 가 가리키는 기본 위치
    teach(bank)
    assert get_diagnosis(run.run_id, engine).diagnosed_by == "reasoning_bank"
    assert rediagnose(run.run_id, engine).diagnosed_by == "rule"


# -- 엔진: 실제로 회복됐을 때만 배운다 ---------------------------------------


def make_engine(tmp_path: Path, bank, runner) -> Engine:
    return Engine(RunStore(tmp_path / "w.sqlite3"), {StageName.DISCOVERY: runner}, memory=bank)


def failing_then_ok():
    calls = {"n": 0}

    def runner(run: RunStatus, ctx: RunContext) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("VPN 터널 10.0.0.5 응답 없음")
        return {"ok": True}

    return runner


def test_engine_learns_when_the_diagnosed_stage_recovers_after_a_retry(tmp_path, bank):
    engine = make_engine(tmp_path, bank, failing_then_ok())
    run = engine.advance(engine.create_run(REQUEST).run_id)
    assert run.state == WorkflowState.FAILED

    message = run.stage(StageName.DISCOVERY).message
    diagnose_run(run, bank=bank)  # 운영자가 진단을 봤다
    engine.resume(run.run_id)
    assert engine.advance(run.run_id).stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED

    episode = bank.lookup("discovery", "gcp", message)
    assert episode is not None and episode.success_count == 1 and episode.action == "retry"


def test_engine_does_not_learn_from_a_retry_nobody_diagnosed(tmp_path, bank):
    engine = make_engine(tmp_path, bank, failing_then_ok())
    run = engine.advance(engine.create_run(REQUEST).run_id)
    message = run.stage(StageName.DISCOVERY).message
    engine.resume(run.run_id)
    engine.advance(run.run_id)
    assert bank.lookup("discovery", "gcp", message) is None


def test_engine_does_not_learn_when_the_retry_fails_again(tmp_path, bank):
    def always_fails(run: RunStatus, ctx: RunContext) -> dict:
        raise TimeoutError("VPN 터널 10.0.0.5 응답 없음")

    engine = make_engine(tmp_path, bank, always_fails)
    run = engine.advance(engine.create_run(REQUEST).run_id)
    message = run.stage(StageName.DISCOVERY).message
    diagnose_run(run, bank=bank)
    engine.resume(run.run_id)
    engine.advance(run.run_id)
    assert bank.lookup("discovery", "gcp", message) is None


def test_engine_does_not_learn_when_the_run_was_aborted_during_the_retry(tmp_path, bank):
    holder = {}
    calls = {"n": 0}

    def runner(run: RunStatus, ctx: RunContext) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("VPN 터널 10.0.0.5 응답 없음")
        holder["engine"].abort(run.run_id, "운영자 중단")  # 재시도 중에 중단됐는데 모듈은 성공을 돌려줌
        return {"ok": True}

    engine = make_engine(tmp_path, bank, runner)
    holder["engine"] = engine
    run = engine.advance(engine.create_run(REQUEST).run_id)
    message = run.stage(StageName.DISCOVERY).message
    diagnose_run(run, bank=bank)
    engine.resume(run.run_id)
    after = engine.advance(run.run_id)

    assert after.state == WorkflowState.ABORTED
    assert bank.lookup("discovery", "gcp", message) is None


def test_engine_learns_a_manual_input_recovery_with_that_action(tmp_path, bank):
    def broken(run: RunStatus, ctx: RunContext) -> dict:
        raise RuntimeError("AWS 조회 실패")

    engine = make_engine(tmp_path, bank, broken)
    run = engine.advance(engine.create_run(REQUEST).run_id)
    message = run.stage(StageName.DISCOVERY).message
    diagnose_run(run, bank=bank)
    engine.submit_manual(run.run_id, StageName.DISCOVERY, ManualStageInput(output={}))
    episode = bank.lookup("discovery", "gcp", message)
    assert episode is not None and episode.action == "manual_input"


def test_failing_again_after_following_a_memory_lowers_its_record(tmp_path, bank):
    def always_fails(run: RunStatus, ctx: RunContext) -> dict:
        raise TimeoutError("VPN 터널 10.0.0.5 응답 없음")

    engine = make_engine(tmp_path, bank, always_fails)
    first = engine.advance(engine.create_run(REQUEST).run_id)
    message = first.stage(StageName.DISCOVERY).message
    teach(bank, error=message, stage="discovery")

    second = engine.advance(engine.create_run(REQUEST).run_id)
    assert diagnose_run(second, bank=bank).diagnosed_by == "reasoning_bank"  # 기억을 따름
    engine.resume(second.run_id)
    engine.advance(second.run_id)  # 다시 실패
    assert bank.lookup("discovery", "gcp", message).fail_count == 1


def test_a_broken_bank_never_breaks_the_engine(tmp_path):
    class Broken:
        def resolve(self, *a, **k):
            raise RuntimeError("boom")

        fail = resolve

    engine = make_engine(tmp_path, Broken(), failing_then_ok())
    run = engine.advance(engine.create_run(REQUEST).run_id)
    engine.resume(run.run_id)
    assert engine.advance(run.run_id).stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED


def test_engine_without_memory_behaves_as_before(tmp_path):
    engine = Engine(RunStore(tmp_path / "w.sqlite3"), {StageName.DISCOVERY: failing_then_ok()})
    run = engine.advance(engine.create_run(REQUEST).run_id)
    engine.resume(run.run_id)
    assert engine.advance(run.run_id).stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED


def test_fingerprint_matches_between_engine_failure_text_and_diagnosis_text(tmp_path, bank):
    engine = make_engine(tmp_path, bank, failing_then_ok())
    run = engine.advance(engine.create_run(REQUEST).run_id)
    stage_message = run.stage(StageName.DISCOVERY).message
    assert fingerprint(stage_message) == fingerprint(run.stage(StageName.DISCOVERY).message)
    assert stage_message.startswith("TimeoutError")
