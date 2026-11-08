"""실제 Engine·RUNNERS 로 전체 흐름을 확인합니다. 가짜는 모듈 경계(assessment, provisioning, db_migration, 에이전트)에만 둡니다."""

import logging
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from functools import partial
from http.client import IncompleteRead
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.features.db_migration.client import AgentError
from app.features.db_migration.guide_workflow import to_job_id
from app.features.provisioning.schemas import ProvisioningInput
from app.features.workflows.engine import Engine, InvalidTransition
from app.features.workflows.readiness import cutover_readiness
from app.features.workflows.router import get_engine, get_preflight
from app.features.workflows.runners import CUTOVER_SEQUENCE, RUNNERS, run_cutover
from app.features.workflows.schemas import (
    ManualStageInput,
    MigrationRequest,
    PreflightCheck,
    PreflightReport,
    StageName,
    WorkflowState,
)
from app.features.workflows.store import RunStore
from app.main import app

APP_URL = "https://target-app.run.app"
TARGET_INFRA = {"region": "asia-northeast3", "service_name": "target-app"}
REQUEST = MigrationRequest(
    github_repo_url="https://github.com/example/target-app",
    aws_region="ap-northeast-2",
    aws_credentials_ref="secret://mig-001/source-cloud",
    db_admin_ref="secret://mig-001/source-db-admin",
)
BODY = {
    "github_repo_url": REQUEST.github_repo_url,
    "aws_region": REQUEST.aws_region,
    "aws_credentials_ref": REQUEST.aws_credentials_ref,
    "db_admin_ref": REQUEST.db_admin_ref,
}
ENV_VARS = (
    "ALPHA_CDC_SIMULATION",
    "ALPHA_CUTOVER_MAX_LAG_BYTES",
    "ALPHA_PROBE_URL",
    "WORKER_URL",
    "WORKER_TOKEN",
    "WORKER_TOKEN_FILE",
)
STABILIZED = {
    "state": "running",
    "phase": "stabilizing",
    "progress": 90,
    "message": "안정화 관찰 중",
    "metrics": {"traffic_mode": "TARGET", "cutover_ms": 800.0},
    "evidence": {"cutover": {"steps": [{"step": name} for name in CUTOVER_SEQUENCE]}},
}
ROLLED_BACK = {
    "state": "waiting_approval",
    "phase": "cutover_ready",
    "progress": 70,
    "message": "전환 실패, 원본으로 재개: marker timeout",
    "evidence": {"cutover": {"steps": [{"step": "hold"}, {"step": "failed", "error": "marker timeout"}]}},
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    monkeypatch.setattr("app.core.secrets.has_secret", lambda ref: False)


@pytest.fixture
def world(monkeypatch) -> Iterator[SimpleNamespace]:
    """모듈 경계의 가짜와 호출 기록입니다. db_phase 를 보고한 뒤 CDC 는 계속 도는 것처럼 대기합니다."""
    state = SimpleNamespace(
        order=[], provisioning_inputs=[], cutover_calls=[], jobs=[STABILIZED], db_phase="streaming", cutover_error=None
    )
    keep_streaming = threading.Event()

    def assessment_run(inp, ctx):
        state.order.append("planning")
        return {"source": {"tables": ["public.users"]}, "plan": {"target_infra": TARGET_INFRA}, "gaps": []}

    package = ModuleType("app.features.assessment")
    package.service = SimpleNamespace(run=assessment_run)
    schemas = ModuleType("app.features.assessment.schemas")
    schemas.AssessmentInput = lambda **fields: SimpleNamespace(**fields)
    monkeypatch.setitem(sys.modules, "app.features.assessment", package)
    monkeypatch.setitem(sys.modules, "app.features.assessment.schemas", schemas)

    def provisioning_run(inp, ctx):
        state.order.append("provisioning")
        state.provisioning_inputs.append(inp)
        return {"target": {"db": {"private_ip": "10.31.0.5"}}, "applied": True}

    def db_migration_run(inp, ctx):
        state.order.append("db_migration")
        ctx.report("running", 50, {"phase": state.db_phase}, "CDC 동기화 중")
        keep_streaming.wait(timeout=30)
        return {"state": "stopped"}

    def deploy_app(inp, ctx):
        state.order.append("app_deploy")
        return {"app_url": APP_URL, "healthy": True}

    monkeypatch.setattr("app.features.provisioning.service.run", provisioning_run)
    monkeypatch.setattr("app.features.provisioning.service.deploy_app", deploy_app)
    monkeypatch.setattr("app.features.db_migration.service.run", db_migration_run)

    class FakeAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            state.cutover_calls.append((job_id, approved_by))
            if state.cutover_error:
                raise state.cutover_error
            return {"accepted": True}

        def get_job(self, job_id):
            job = state.jobs.pop(0) if len(state.jobs) > 1 else state.jobs[0]
            if isinstance(job, Exception):
                raise job
            return job

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)
    # 롤백 시험의 기본값은 Worker 가 원본을 확인한 경우입니다.
    monkeypatch.setattr("app.features.workflows.runners._read_worker_status", lambda: CONFIRMED_ON_SOURCE)
    try:
        yield state
    finally:
        keep_streaming.set()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(RunStore(tmp_path / "workflows.sqlite3"), RUNNERS)


def _to_cutover_gate(engine: Engine) -> str:
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    engine.approve(run_id, "plan", approver="계획 승인자")
    run = engine.advance(run_id)
    assert (run.state, run.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    return run_id


def test_fresh_run_passes_discovery_and_waits_for_plan_approval(engine, world):
    run = engine.advance(engine.create_run(REQUEST).run_id)

    assert (run.state, run.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "plan")
    assert run.current_stage == StageName.PROVISIONING
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED
    assert run.outputs["discovery"]["merged_into"] == "planning"
    assert world.order == ["planning"]


def test_approving_the_plan_passes_target_infra_to_provisioning(engine, world):
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id

    engine.approve(run_id, "plan")
    engine.advance(run_id)

    inp = world.provisioning_inputs[0]
    assert isinstance(inp, ProvisioningInput)
    assert inp.run_id == run_id
    assert inp.target_infra == TARGET_INFRA


@pytest.mark.parametrize("phase", ["streaming", "cutover_ready"])
def test_cdc_ready_phase_starts_app_deploy_and_stops_at_cutover_gate(engine, world, phase):
    world.db_phase = phase

    run_id = _to_cutover_gate(engine)

    run = engine.get(run_id)
    assert world.order == ["planning", "provisioning", "db_migration", "app_deploy"]
    assert run.current_stage == StageName.CUTOVER
    assert run.stage(StageName.DB_MIGRATION).state == WorkflowState.SUCCEEDED
    assert run.outputs["db_migration"]["phase"] == phase
    assert run.outputs["app_deploy"]["app_url"] == APP_URL
    assert run.stage(StageName.CUTOVER).state == WorkflowState.PENDING
    assert world.cutover_calls == []


def test_approving_cutover_sends_the_approver_to_the_agent(engine, world):
    run_id = _to_cutover_gate(engine)

    engine.approve(run_id, "cutover", "확인: 이상 없음", approver="박준서", acknowledge_unknown=True)
    run = engine.advance(run_id)

    assert world.cutover_calls == [(to_job_id(run_id), "박준서")]
    assert run.state == WorkflowState.SUCCEEDED
    metrics = run.stage(StageName.CUTOVER).metrics
    assert metrics["serving"] == "target"
    assert metrics["cutover_seconds"] == 0.8
    assert metrics["cutover_seconds_source"] == "agent"
    assert metrics["cutover_sequence"] == "VERIFIED"
    assert "failed_requests" not in metrics


def test_rollback_then_resume_needs_fresh_approval_then_succeeds(engine, world):
    run_id = _to_cutover_gate(engine)
    world.jobs = [ROLLED_BACK]

    engine.approve(run_id, "cutover", approver="박준서", acknowledge_unknown=True)
    failed = engine.advance(run_id)

    assert failed.state == WorkflowState.FAILED
    assert failed.approvals == ["plan"]
    assert len(world.cutover_calls) == 1
    cutover = failed.stage(StageName.CUTOVER)
    assert "컷오버 실패로 원본으로 롤백되었습니다" in cutover.message
    assert cutover.metrics["cutover_steps"][-1] == {"step": "failed", "error": "marker timeout"}
    assert "cutover_sequence" not in cutover.metrics

    engine.resume(run_id)
    waiting = engine.advance(run_id)

    assert (waiting.state, waiting.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    assert len(world.cutover_calls) == 1

    world.jobs = [STABILIZED]
    engine.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    done = engine.advance(run_id)

    assert done.state == WorkflowState.SUCCEEDED
    assert [call[1] for call in world.cutover_calls] == ["박준서", "이주호"]
    assert [r.approver for r in done.approval_records if r.gate == "cutover"] == ["박준서", "이주호"]
    assert done.approvals == ["plan", "cutover"]
    metrics = done.stage(StageName.CUTOVER).metrics
    assert metrics["cutover_sequence"] == "VERIFIED"
    assert metrics["cutover_steps"] == [{"step": name} for name in CUTOVER_SEQUENCE]


# -- 전환이 끝났을 수 있는 실패 -------------------------------------------------------

LOST = httpx.ReadTimeout("db.internal.example password=hunter2")
WORKING = {"state": "running", "phase": "cutover", "progress": 75, "message": "쓰기 인계 시작"}
POST_SWITCH_FAILED = {
    "state": "failed",
    "phase": "cutover",
    "progress": 75,
    "message": "목적지로 전환 후 오류, 수동 확인 필요: release timeout",
}
NOT_READY = {"state": "running", "phase": "streaming", "progress": 50, "message": "CDC 동기화 중"}
ALREADY_RUNNING = AgentError(409, "job is running/stabilizing, not waiting for cutover approval")


@pytest.fixture
def fast(tmp_path: Path) -> Engine:
    """폴링을 빠르게 한 실제 run_cutover 엔진입니다."""
    runners = {**RUNNERS, StageName.CUTOVER: partial(run_cutover, poll_interval_s=0.001, timeout_s=0.05)}
    return Engine(RunStore(tmp_path / "fast.sqlite3"), runners)


def _approve_cutover(engine: Engine, jobs: list, world, approver: str = "박준서"):
    run_id = _to_cutover_gate(engine)
    world.jobs = jobs
    engine.approve(run_id, "cutover", approver=approver, acknowledge_unknown=True)
    return run_id, engine.advance(run_id)


def _outcome(run) -> str | None:
    return run.stage(StageName.CUTOVER).metrics.get("cutover_outcome")


def test_one_lost_status_read_does_not_fail_a_completed_switch(fast, world):
    _, run = _approve_cutover(fast, [LOST, STABILIZED], world)

    assert run.state == WorkflowState.SUCCEEDED
    assert len(world.cutover_calls) == 1
    assert _outcome(run) == "switched"
    assert run.stage(StageName.CUTOVER).metrics["cutover_sequence"] == "VERIFIED"


@pytest.mark.parametrize("bad", ["not json", ["a", "list"], None, ValueError("non-JSON response")])
def test_unreadable_status_is_retried_until_the_deadline(fast, world, bad):
    _, run = _approve_cutover(fast, [bad, STABILIZED], world)

    assert run.state == WorkflowState.SUCCEEDED


@pytest.mark.parametrize("jobs", [[WORKING], [LOST]], ids=["still-working", "lost-contact"])
def test_timeout_is_not_a_retryable_failure_and_keeps_manual_completion(fast, world, jobs):
    run_id, run = _approve_cutover(fast, jobs, world)

    assert run.state == WorkflowState.FAILED
    assert _outcome(run) == "unknown"
    assert run.approvals == ["plan", "cutover"]
    assert "수동 확인 필요" in run.message
    assert "hunter2" not in run.message
    assert "hunter2" not in (run.stage(StageName.CUTOVER).message or "")

    done = fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}, note="직접 확인"))
    assert done.stage(StageName.CUTOVER).manual is True


def test_retry_after_lost_contact_verifies_instead_of_switching_again(fast, world):
    run_id, _ = _approve_cutover(fast, [LOST], world)

    fast.resume(run_id)
    waiting = fast.advance(run_id)
    assert (waiting.state, waiting.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    assert _outcome(waiting) == "unknown"

    world.cutover_error = ALREADY_RUNNING
    world.jobs = [STABILIZED]
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    done = fast.advance(run_id)

    assert done.state == WorkflowState.SUCCEEDED
    assert _outcome(done) == "switched"
    assert done.stage(StageName.CUTOVER).metrics["cutover_sequence"] == "VERIFIED"


def test_post_switch_failure_never_offers_retry_and_stays_recoverable(fast, world):
    run_id, failed = _approve_cutover(fast, [POST_SWITCH_FAILED], world)

    assert failed.state == WorkflowState.FAILED
    assert _outcome(failed) == "post_switch_failed"
    assert failed.approvals == ["plan", "cutover"]
    assert "수동 확인 필요" in failed.message
    assert "컷오버 실패 (failed)" in failed.stage(StageName.CUTOVER).message

    # 다시 눌러도 에이전트가 409 로 막으므로 두 번째 전환은 없고, 같은 판정으로 돌아옵니다.
    fast.resume(run_id)
    fast.advance(run_id)
    world.cutover_error = AgentError(409, "job is failed/cutover, not waiting for cutover approval")
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    again = fast.advance(run_id)

    assert _outcome(again) == "post_switch_failed"
    assert again.approvals == ["plan", "cutover"]
    done = fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}))
    assert done.stage(StageName.CUTOVER).manual is True


def _fail_then_retry(engine: Engine, first_job, world) -> str:
    run_id, _ = _approve_cutover(engine, [first_job], world)
    engine.resume(run_id)
    engine.advance(run_id)
    return run_id


@pytest.mark.parametrize(
    ("first_job", "expected"), [(POST_SWITCH_FAILED, "post_switch_failed"), (LOST, "unknown")], ids=["post-switch", "lost"]
)
@pytest.mark.parametrize(
    "refusal", [AgentError(403, "Access denied"), AgentError(409, "job is running/streaming")], ids=["403", "409"]
)
def test_refused_retry_does_not_erase_that_traffic_may_have_moved(fast, world, first_job, expected, refusal):
    run_id = _fail_then_retry(fast, first_job, world)

    world.cutover_error = refusal
    world.jobs = [NOT_READY]
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    run = fast.advance(run_id)

    assert _outcome(run) == expected
    assert run.approvals == ["plan", "cutover"]
    assert "수동 확인 필요" in run.message


@pytest.mark.parametrize(
    ("first_job", "expected"), [(POST_SWITCH_FAILED, "post_switch_failed"), (LOST, "unknown")], ids=["post-switch", "lost"]
)
def test_retry_without_agent_url_does_not_erase_that_traffic_may_have_moved(fast, world, monkeypatch, first_job, expected):
    run_id = _fail_then_retry(fast, first_job, world)

    monkeypatch.delenv("CDC_AGENT_URL")
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    run = fast.advance(run_id)

    assert _outcome(run) == expected
    assert run.approvals == ["plan", "cutover"]


def test_refused_retry_after_a_confirmed_rollback_is_still_not_started(fast, world):
    run_id = _fail_then_retry(fast, ROLLED_BACK, world)

    world.cutover_error = AgentError(403, "Access denied")
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    run = fast.advance(run_id)

    assert _outcome(run) == "not_started"
    assert run.approvals == ["plan"]


def test_rollback_is_recorded_and_a_stale_outcome_does_not_survive_the_next_attempt(fast, world):
    run_id, rolled_back = _approve_cutover(fast, [ROLLED_BACK], world)
    assert _outcome(rolled_back) == "rolled_back"
    assert rolled_back.approvals == ["plan"]

    fast.resume(run_id)
    waiting = fast.advance(run_id)
    assert _outcome(waiting) == "rolled_back"

    world.jobs = [LOST]
    fast.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    lost = fast.advance(run_id)

    assert _outcome(lost) == "unknown"
    assert lost.approvals == ["plan", "cutover"]


@pytest.mark.parametrize(
    "refusal", [AgentError(409, "job is running/streaming"), AgentError(403, "Access denied")], ids=["409", "403"]
)
def test_refused_request_is_not_started_and_can_be_retried(fast, world, refusal):
    world.cutover_error = refusal
    _, run = _approve_cutover(fast, [NOT_READY], world)

    assert run.state == WorkflowState.FAILED
    assert _outcome(run) == "not_started"
    assert run.approvals == ["plan"]
    assert "거부" in run.stage(StageName.CUTOVER).message
    assert "전환 승인 ②" in run.message


def test_refused_request_while_the_job_failed_before_cutover_is_not_started(fast, world):
    world.cutover_error = AgentError(409, "job is failed/streaming")
    _, run = _approve_cutover(
        fast, [{"state": "failed", "phase": "streaming", "progress": 50, "message": "동기화 중단"}], world
    )

    assert _outcome(run) == "not_started"
    assert run.approvals == ["plan"]


@pytest.mark.parametrize(
    "error", [AgentError(502, "bad gateway"), httpx.ConnectTimeout("no route")], ids=["502", "transport"]
)
def test_lost_post_response_is_judged_from_the_job_not_assumed_failed(fast, world, error):
    world.cutover_error = error
    _, run = _approve_cutover(fast, [STABILIZED], world)

    assert run.state == WorkflowState.SUCCEEDED
    assert _outcome(run) == "switched"


def test_lost_post_response_with_no_later_contact_stays_unknown(fast, world):
    world.cutover_error = httpx.ConnectTimeout("no route")
    _, run = _approve_cutover(fast, [LOST], world)

    assert _outcome(run) == "unknown"
    assert run.approvals == ["plan", "cutover"]


def test_missing_agent_url_is_not_started(fast, world, monkeypatch):
    run_id = _to_cutover_gate(fast)
    monkeypatch.delenv("CDC_AGENT_URL")
    fast.approve(run_id, "cutover", acknowledge_unknown=True)

    run = fast.advance(run_id)

    assert _outcome(run) == "not_started"
    assert world.cutover_calls == []


@pytest.mark.parametrize(
    "job",
    [
        {**STABILIZED, "metrics": {"traffic_mode": "TARGET", "cutover_ms": 10**400}},
        {**STABILIZED, "evidence": {"cutover": {"steps": [{"step": "hold\ud800"}]}}},
        {**STABILIZED, "evidence": {"cutover": {"steps": [{"step": "hold", "error": "\udfff"}]}}},
        {**STABILIZED, "message": "완료\ud800"},
        {**STABILIZED, "metrics": {"traffic_mode": "TARGET", "note\ud800": "\ud800"}},
    ],
    ids=["overflow", "surrogate-step", "surrogate-error", "surrogate-message", "surrogate-metric"],
)
def test_malformed_evidence_after_the_switch_still_succeeds(fast, world, job):
    run_id, run = _approve_cutover(fast, [job], world)

    assert run.state == WorkflowState.SUCCEEDED
    assert _outcome(run) == "switched"
    assert run.approvals == ["plan", "cutover"]
    assert fast.get(run_id).state == WorkflowState.SUCCEEDED  # 저장된 row 를 다시 읽을 수 있어야 합니다


def test_overflowing_duration_is_reported_as_not_provided(fast, world):
    job = {**STABILIZED, "metrics": {"traffic_mode": "TARGET", "cutover_ms": 10**400}}
    _, run = _approve_cutover(fast, [job], world)

    metrics = run.stage(StageName.CUTOVER).metrics
    assert "cutover_seconds" not in metrics
    assert "cutover_seconds_source" not in metrics
    assert metrics["serving"] == "target"


@pytest.mark.parametrize("broken", ["_cutover_metrics", "_verify_cutover_sequence"])
def test_evidence_processing_failure_degrades_instead_of_failing_the_switch(fast, world, monkeypatch, broken):
    run_id = _to_cutover_gate(fast)

    def explode(*args):
        raise RuntimeError("증적 처리 오류")

    monkeypatch.setattr(f"app.features.workflows.runners.{broken}", explode)
    world.jobs = [STABILIZED]
    fast.approve(run_id, "cutover", approver="박준서", acknowledge_unknown=True)

    run = fast.advance(run_id)

    assert run.state == WorkflowState.SUCCEEDED
    assert _outcome(run) == "switched"


def test_failed_report_of_the_final_result_keeps_the_conservative_outcome(tmp_path, world):
    class LosesFinalReport:
        def __init__(self, ctx):
            self._ctx = ctx

        def report(self, state, progress, metrics=None, message=None):
            if (metrics or {}).get("cutover_outcome") == "switched":
                raise OSError("disk full")
            self._ctx.report(state, progress, metrics, message)

    def cutover(run, ctx):
        return run_cutover(run, LosesFinalReport(ctx), poll_interval_s=0.001, timeout_s=0.05)

    engine = Engine(RunStore(tmp_path / "lost.sqlite3"), {**RUNNERS, StageName.CUTOVER: cutover})
    _, run = _approve_cutover(engine, [STABILIZED], world)

    assert run.state == WorkflowState.FAILED
    assert _outcome(run) == "unknown"
    assert run.approvals == ["plan", "cutover"]


# -- HTTP --------------------------------------------------------------------------


MEASURED_AT = "2026-11-06T01:02:03Z"


def _report(run, verdict="no_go"):
    check = PreflightCheck(id="data_match", label="데이터 일치", status="fail", value=2)
    return PreflightReport(run_id=run.run_id, verdict=verdict, checks=[check], measured_at=datetime.now(timezone.utc))


class Evaluator:
    def __init__(self, verdict="no_go", error=None):
        self.calls = 0
        self.verdict = verdict
        self.error = error

    def __call__(self, run):
        self.calls += 1
        if self.error:
            raise self.error
        return _report(run, self.verdict)


@pytest.fixture
def api(tmp_path) -> Iterator[SimpleNamespace]:
    """모듈 연결 없는 엔진입니다. 수동 입력으로 전환 승인 대기까지 갑니다."""
    engine = Engine(RunStore(tmp_path / "api.sqlite3"))
    evaluator = Evaluator()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_preflight] = lambda: evaluator
    try:
        yield SimpleNamespace(client=TestClient(app), evaluator=evaluator, engine=engine)
    finally:
        app.dependency_overrides.clear()


def _post_manual(client, run_id, stage, output=None):
    res = client.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": output or {"stage": stage}})
    assert res.status_code == 200


def _api_to_plan_gate(client) -> str:
    run_id = client.post("/runs", json=BODY).json()["run_id"]
    for stage in ("discovery", "planning"):
        _post_manual(client, run_id, stage)
    return run_id


def _api_to_cutover_gate(client) -> str:
    run_id = _api_to_plan_gate(client)
    assert client.post(f"/runs/{run_id}/approve", json={"gate": "plan"}).status_code == 200
    for stage in ("provisioning", "db_migration"):
        _post_manual(client, run_id, stage)
    _post_manual(client, run_id, "app_deploy", {"app_url": APP_URL})
    run = client.get(f"/runs/{run_id}").json()
    assert (run["state"], run["waiting_gate"]) == ("waiting_approval", "cutover")
    return run_id


def test_preflight_unknown_run_is_404(api):
    assert api.client.get("/runs/mig-missing/preflight").status_code == 404
    assert api.evaluator.calls == 0


def test_preflight_conflicts_unless_waiting_at_cutover_gate(api):
    run_id = api.client.post("/runs", json=BODY).json()["run_id"]
    assert api.client.get(f"/runs/{run_id}/preflight").status_code == 409  # paused

    run_id = _api_to_plan_gate(api.client)
    assert api.client.get(f"/runs/{run_id}/preflight").status_code == 409  # 계획 승인 대기
    assert api.evaluator.calls == 0


def test_preflight_returns_the_report_at_cutover_gate(api):
    run_id = _api_to_cutover_gate(api.client)

    res = api.client.get(f"/runs/{run_id}/preflight")

    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == run_id
    assert body["gate"] == "cutover"
    assert body["verdict"] == "no_go"
    assert body["advisory"] is True
    assert "simulated" not in body
    assert body["checks"][0]["id"] == "data_match"
    assert api.evaluator.calls == 1


def test_real_evaluator_never_calls_a_manual_app_url(api, monkeypatch):
    del app.dependency_overrides[get_preflight]
    monkeypatch.delenv("CDC_AGENT_URL")  # 실제 네트워크 호출 없이 에이전트 점검은 unknown 입니다
    calls = []
    monkeypatch.setattr(
        "app.features.workflows.preflight._http_status", lambda url: calls.append(url) or 200
    )
    run_id = _api_to_cutover_gate(api.client)

    body = api.client.get(f"/runs/{run_id}/preflight").json()

    assert calls == []
    statuses = {c["id"]: c["status"] for c in body["checks"]}
    assert statuses["target_ready"] == "unknown"
    assert [c["id"] for c in body["checks"]] == [
        "job_ready",
        "db_connectivity",
        "data_match",
        "replication_lag",
        "target_ready",
        "worker_target",
    ]


def test_approve_succeeds_with_no_go_and_records_the_verdict(api):
    run_id = _api_to_cutover_gate(api.client)

    res = api.client.post(
        f"/runs/{run_id}/approve",
        json={
            "gate": "cutover",
            "acknowledge_unknown": True,
            "comment": "감수함",
            "approver": " 박준서 ",
            "preflight_verdict": "no_go",
            "preflight_measured_at": MEASURED_AT,
        },
    )

    assert res.status_code == 200
    record = res.json()["approval_records"][-1]
    assert record["gate"] == "cutover"
    assert record["approver"] == "박준서"
    assert record["comment"] == "감수함"
    assert record["preflight_verdict"] == "no_go"
    assert api.evaluator.calls == 0


def test_cutover_approve_never_calls_the_evaluator_even_if_it_would_raise(api, caplog):
    run_id = _api_to_cutover_gate(api.client)
    api.evaluator.error = RuntimeError("db.internal.example password=hunter2")

    with caplog.at_level(logging.DEBUG):
        res = api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True})

    assert res.status_code == 200
    assert api.evaluator.calls == 0
    assert res.json()["approval_records"][-1]["preflight_verdict"] is None
    assert "hunter2" not in caplog.text


def test_cutover_approve_makes_no_network_call(api, monkeypatch):
    calls = []
    del app.dependency_overrides[get_preflight]
    monkeypatch.setattr("app.features.workflows.router.evaluate_cutover", lambda *a, **k: calls.append("evaluate"))
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: calls.append("connect"))
    run_id = _api_to_cutover_gate(api.client)

    assert api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True}).status_code == 200
    assert calls == []


@pytest.mark.parametrize("verdict", ["go", "no_go", "unverified"])
def test_cutover_approve_records_exactly_what_the_client_sent(api, verdict):
    run_id = _api_to_cutover_gate(api.client)

    res = api.client.post(
        f"/runs/{run_id}/approve",
        json={"gate": "cutover", "acknowledge_unknown": True, "preflight_verdict": verdict, "preflight_measured_at": MEASURED_AT},
    )

    record = res.json()["approval_records"][-1]
    assert record["preflight_verdict"] == verdict
    assert datetime.fromisoformat(record["preflight_measured_at"]) == datetime.fromisoformat(MEASURED_AT)


def test_cutover_approve_without_a_report_records_nulls(api):
    run_id = _api_to_cutover_gate(api.client)

    res = api.client.post(
        f"/runs/{run_id}/approve",
        json={"gate": "cutover", "acknowledge_unknown": True, "preflight_verdict": None, "preflight_measured_at": None},
    )

    record = res.json()["approval_records"][-1]
    assert (record["preflight_verdict"], record["preflight_measured_at"]) == (None, None)


@pytest.mark.parametrize(
    "fields", [{"preflight_verdict": "maybe"}, {"preflight_measured_at": "어제"}], ids=["verdict", "time"]
)
def test_approve_rejects_malformed_preflight_values(api, fields):
    run_id = _api_to_cutover_gate(api.client)

    assert api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True, **fields}).status_code == 422
    assert api.client.get(f"/runs/{run_id}").json()["waiting_gate"] == "cutover"


def test_plan_approve_discards_preflight_values(api):
    run_id = _api_to_plan_gate(api.client)

    res = api.client.post(
        f"/runs/{run_id}/approve",
        json={"gate": "plan", "preflight_verdict": "go", "preflight_measured_at": MEASURED_AT},
    )

    assert res.status_code == 200
    record = res.json()["approval_records"][-1]
    assert (record["preflight_verdict"], record["preflight_measured_at"]) == (None, None)


def test_plan_approval_does_not_run_the_evaluator(api):
    run_id = _api_to_plan_gate(api.client)

    res = api.client.post(f"/runs/{run_id}/approve", json={"gate": "plan", "approver": "이주호"})

    assert res.status_code == 200
    assert api.evaluator.calls == 0
    record = res.json()["approval_records"][-1]
    assert (record["gate"], record["approver"], record["preflight_verdict"]) == ("plan", "이주호", None)


def test_cutover_approval_when_not_waiting_conflicts_without_evaluating(api):
    run_id = _api_to_plan_gate(api.client)

    assert api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True}).status_code == 409
    assert api.evaluator.calls == 0
    assert api.client.post("/runs/mig-missing/approve", json={"gate": "cutover", "acknowledge_unknown": True}).status_code == 404


def test_approve_validates_approver_length(api):
    run_id = _api_to_cutover_gate(api.client)

    res = api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True, "approver": "x" * 101})

    assert res.status_code == 422
    assert api.client.get(f"/runs/{run_id}").json()["waiting_gate"] == "cutover"


def test_blank_approver_is_recorded_as_none(api):
    run_id = _api_to_cutover_gate(api.client)
    res = api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True, "approver": "   "})
    assert res.json()["approval_records"][-1]["approver"] is None


def test_openapi_lists_preflight_route(api):
    assert "/runs/{run_id}/preflight" in api.client.get("/openapi.json").json()["paths"]


def test_full_flow_over_http_with_real_runners(tmp_path, world):
    engine = Engine(RunStore(tmp_path / "flow.sqlite3"), RUNNERS)
    evaluator = Evaluator(verdict="go")
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_preflight] = lambda: evaluator
    try:
        client = TestClient(app)
        run_id = client.post("/runs", json=BODY).json()["run_id"]
        assert client.get(f"/runs/{run_id}").json()["waiting_gate"] == "plan"

        client.post(f"/runs/{run_id}/approve", json={"gate": "plan", "approver": "이주호"})
        assert client.get(f"/runs/{run_id}").json()["waiting_gate"] == "cutover"
        shown = client.get(f"/runs/{run_id}/preflight").json()
        assert shown["verdict"] == "go"

        # 화면처럼 표시한 점검 결과를 승인 요청에 실어 보냅니다.
        client.post(
            f"/runs/{run_id}/approve",
            json={
                "gate": "cutover",
                "acknowledge_unknown": True,
                "approver": "박준서",
                "preflight_verdict": shown["verdict"],
                "preflight_measured_at": shown["measured_at"],
            },
        )

        run = client.get(f"/runs/{run_id}").json()
        assert run["state"] == "succeeded"
        assert world.cutover_calls == [(to_job_id(run_id), "박준서")]
        assert run["approval_records"][-1]["preflight_verdict"] == "go"
    finally:
        app.dependency_overrides.clear()


# -- 승인 조건(서버 차단)과 실시간 점검(참고)을 함께 쓰는 경우 ---------------------------------


READY_METRICS = {"phase": "cutover_ready", "copy_percent": 100, "replication_lag_bytes": 0, "mismatch": 0}


def _set_gate_inputs(api, run_id, metrics, *, is_app_healthy=None):
    """승인 조건이 읽는 저장값을 정합니다. is_app_healthy 를 주면 수동 입력이 아닌 실제 배포 결과처럼 둡니다."""
    from app.features.workflows.context import with_stage

    def change(run):
        updated = with_stage(run, StageName.DB_MIGRATION, metrics=metrics)
        if is_app_healthy is None:
            return updated
        updated = with_stage(updated, StageName.APP_DEPLOY, manual=False)
        return updated.model_copy(
            update={"outputs": {**updated.outputs, "app_deploy": {"healthy": is_app_healthy}}}
        )

    api.engine._store.update(run_id, change)


def _approve(api, run_id, **body):
    return api.client.post(f"/runs/{run_id}/approve", json={"gate": "cutover", **body})


def test_failed_condition_blocks_even_with_acknowledgement_and_a_go_screen(api):
    run_id = _api_to_cutover_gate(api.client)
    _set_gate_inputs(api, run_id, {**READY_METRICS, "replication_lag_bytes": 2 * 1024 * 1024}, is_app_healthy=True)

    res = _approve(api, run_id, acknowledge_unknown=True, approver="박준서", preflight_verdict="go")

    assert res.status_code == 409
    run = api.engine.get(run_id)
    assert run.state == WorkflowState.WAITING_APPROVAL
    assert "cutover" not in run.approvals
    assert [r for r in run.approval_records if r.gate == "cutover"] == []


def test_unknown_condition_needs_acknowledgement(api):
    run_id = _api_to_cutover_gate(api.client)

    assert _approve(api, run_id, approver="박준서").status_code == 409

    res = _approve(api, run_id, approver="박준서", acknowledge_unknown=True)
    assert res.status_code == 200
    assert res.json()["approval_records"][-1]["readiness_decision"] == "needs_confirmation"


def test_live_preflight_no_go_does_not_block_when_conditions_pass(api):
    run_id = _api_to_cutover_gate(api.client)
    _set_gate_inputs(api, run_id, READY_METRICS, is_app_healthy=True)

    res = _approve(api, run_id, approver="박준서", preflight_verdict="no_go", preflight_measured_at=MEASURED_AT)

    assert res.status_code == 200
    record = res.json()["approval_records"][-1]
    assert record["readiness_decision"] == "go"
    assert record["preflight_verdict"] == "no_go"
    assert api.evaluator.calls == 0


def test_run_detail_and_preflight_report_their_own_verdicts(api):
    run_id = _api_to_cutover_gate(api.client)
    _set_gate_inputs(api, run_id, READY_METRICS, is_app_healthy=True)

    assert api.client.get(f"/runs/{run_id}").json()["cutover_readiness"]["decision"] == "go"
    assert api.client.get(f"/runs/{run_id}/preflight").json()["verdict"] == "no_go"


def test_plan_approval_records_no_condition_decision(api):
    run_id = _api_to_plan_gate(api.client)

    res = api.client.post(f"/runs/{run_id}/approve", json={"gate": "plan"})

    assert res.json()["approval_records"][-1]["readiness_decision"] is None


def test_old_approval_rows_load_without_a_condition_decision():
    from app.features.workflows.schemas import ApprovalRecord

    record = ApprovalRecord.model_validate({"gate": "cutover", "at": MEASURED_AT})

    assert record.readiness_decision is None


# -- 리뷰에서 재현된 문제: 원본 복귀가 확인되지 않으면 rolled_back 으로 쓰지 않습니다 --------------


CONFIRMED_ON_SOURCE = {"mode": "SOURCE", "next_origin": "SOURCE"}


@pytest.fixture
def worker_status(monkeypatch):
    """Worker 가 돌려줄 상태입니다. None 이면 읽지 못한 경우입니다."""
    holder = SimpleNamespace(value=CONFIRMED_ON_SOURCE)
    monkeypatch.setattr("app.features.workflows.runners._read_worker_status", lambda: holder.value)
    return holder


def test_confirmed_rollback_is_rolled_back(fast, world, worker_status):
    _, run = _approve_cutover(fast, [ROLLED_BACK], world)

    assert _outcome(run) == "rolled_back"
    assert "cutover" not in run.approvals


@pytest.mark.parametrize(
    "status",
    [
        {"mode": "TARGET", "next_origin": "TARGET"},  # 경로 변경 응답만 잃고 release 가 목적지로 보낸 경우
        {"mode": "HOLD", "next_origin": "SOURCE"},
        {"mode": "SOURCE", "next_origin": "TARGET"},
        {},
        None,
    ],
    ids=["on-target", "still-held", "source-but-next-target", "no-fields", "unreadable"],
)
def test_rollback_report_without_worker_confirmation_is_unknown(fast, world, worker_status, status):
    worker_status.value = status

    _, run = _approve_cutover(fast, [ROLLED_BACK], world)

    assert _outcome(run) == "unknown"
    assert run.approvals == ["plan", "cutover"]  # 사람이 확인한 뒤 완료를 기록할 수 있어야 합니다
    assert "수동 확인 필요" in run.message
    assert "롤백되었습니다" not in (run.stage(StageName.CUTOVER).message or "")


def test_rollback_with_a_failed_resume_step_is_unknown(fast, world, worker_status):
    steps = [{"step": "failed", "error": "x"}, {"step": "source_writes_unblocked_failed", "error": "y"}]

    _, run = _approve_cutover(fast, [{**ROLLED_BACK, "evidence": {"cutover": {"steps": steps}}}], world)

    assert _outcome(run) == "unknown"


@pytest.mark.parametrize("error", [httpx.ReadTimeout("lost"), AgentError(502, "bad gateway")], ids=["timeout", "502"])
def test_lost_post_with_job_still_ready_is_not_a_rollback(fast, world, worker_status, error):
    world.cutover_error = error

    _, run = _approve_cutover(fast, [ROLLED_BACK], world)

    assert _outcome(run) == "unknown"
    assert run.approvals == ["plan", "cutover"]


def test_lost_post_that_then_switches_still_succeeds(fast, world, worker_status):
    world.cutover_error = httpx.ReadTimeout("lost")

    _, run = _approve_cutover(fast, [ROLLED_BACK, STABILIZED], world)

    assert run.state == WorkflowState.SUCCEEDED
    assert _outcome(run) == "switched"


def _uncertain_at_gate(fast, world):
    """시간 초과로 결과를 모른 채 재개해 승인 ② 대기로 돌아온 run 입니다."""
    run_id, run = _approve_cutover(fast, [LOST], world)
    assert _outcome(run) == "unknown"
    fast.resume(run_id)
    run = fast.advance(run_id)
    assert run.waiting_gate == "cutover"
    return run_id


def test_uncertain_outcome_has_a_recording_exit_even_when_conditions_fail(fast, world):
    from app.features.workflows.context import with_stage

    run_id = _uncertain_at_gate(fast, world)
    fast._store.update(run_id, lambda r: with_stage(r, StageName.DB_MIGRATION, metrics={"cdc_error": "끝남"}))
    with pytest.raises(InvalidTransition):
        fast.approve(run_id, "cutover", acknowledge_unknown=True)
    calls_before = len(world.cutover_calls)

    fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}, note="에이전트에서 전환 완료 확인"))
    run = fast.advance(run_id)

    assert run.state == WorkflowState.SUCCEEDED
    assert run.stage(StageName.CUTOVER).manual is True
    assert len(world.cutover_calls) == calls_before  # 기록만 하고 전환을 다시 요청하지 않습니다


def test_recording_exit_needs_a_note(fast, world):
    run_id = _uncertain_at_gate(fast, world)

    with pytest.raises(InvalidTransition):
        fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}))


def test_recording_exit_is_refused_when_traffic_is_on_the_source(fast, world, worker_status):
    run_id, _ = _approve_cutover(fast, [ROLLED_BACK], world)
    fast.resume(run_id)
    assert fast.advance(run_id).waiting_gate == "cutover"

    with pytest.raises(InvalidTransition):
        fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}, note="확인"))


def test_recording_exit_is_refused_on_a_first_attempt(fast, world):
    run_id = _to_cutover_gate(fast)

    with pytest.raises(InvalidTransition):
        fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}, note="확인"))


def test_stale_lag_from_a_cutover_attempt_does_not_block_approval(fast, world, worker_status):
    from app.features.workflows.context import with_stage

    job = {**ROLLED_BACK, "metrics": {"replication_lag_bytes": 2 * 1024 * 1024}}
    run_id, _ = _approve_cutover(fast, [job], world)
    fast.resume(run_id)
    fast.advance(run_id)
    fast._store.update(run_id, lambda r: with_stage(r, StageName.DB_MIGRATION, metrics={"replication_lag_bytes": 0}))

    lag = next(c for c in cutover_readiness(fast.get(run_id)).checks if c.id == "lag")

    assert lag.status == "pass"


def test_lag_growing_after_a_cutover_attempt_still_blocks_approval(fast, world, worker_status):
    from app.features.workflows.context import with_stage

    job = {**ROLLED_BACK, "metrics": {"replication_lag_bytes": 0}}
    run_id, _ = _approve_cutover(fast, [job], world)
    fast.resume(run_id)
    fast.advance(run_id)
    fast._store.update(
        run_id, lambda r: with_stage(r, StageName.DB_MIGRATION, metrics={"replication_lag_bytes": 50 * 1024 * 1024})
    )

    with pytest.raises(InvalidTransition):
        fast.approve(run_id, "cutover", acknowledge_unknown=True)


@pytest.mark.parametrize("outcome", ["unknown", "post_switch_failed", "switched"])
def test_cleanup_refuses_when_traffic_may_have_moved(monkeypatch, tmp_path, world, fast, outcome):
    from app.features.workflows.context import with_stage
    from app.features.workflows.runners import clean_up_cdc

    monkeypatch.setattr("app.features.workflows.runners._read_worker_status", lambda: {"mode": "TARGET", "next_origin": "TARGET"})
    stopped = []
    monkeypatch.setattr("app.features.db_migration.service.stop", lambda run_id, agent=None: stopped.append(run_id))
    world.jobs = [{"state": "waiting_approval", "phase": "cutover_ready"}]
    run_id = _to_cutover_gate(fast)
    fast._store.update(run_id, lambda r: with_stage(r, StageName.CUTOVER, metrics={"cutover_outcome": outcome}))

    with pytest.raises(RuntimeError, match="목적지"):
        clean_up_cdc(fast.get(run_id))

    assert stopped == []


def test_cleanup_proceeds_when_worker_confirms_the_source(monkeypatch, world, fast):
    from app.features.workflows.context import with_stage
    from app.features.workflows.runners import clean_up_cdc

    monkeypatch.setattr("app.features.workflows.runners._read_worker_status", lambda: CONFIRMED_ON_SOURCE)
    stopped = []
    monkeypatch.setattr("app.features.db_migration.service.stop", lambda run_id, agent=None: stopped.append(run_id))
    world.jobs = [{"state": "waiting_approval", "phase": "cutover_ready"}]
    run_id = _to_cutover_gate(fast)
    fast._store.update(run_id, lambda r: with_stage(r, StageName.CUTOVER, metrics={"cutover_outcome": "unknown"}))

    assert clean_up_cdc(fast.get(run_id)) is True
    assert stopped == [run_id]


class _RaisingWorker:
    def __init__(self, status):
        self._status = status

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


@pytest.mark.parametrize(
    "status",
    [
        IncompleteRead(b""),
        "not an object",
        {"run_id": "x", "hold_started_at": "t", "request_counts": {"since": "t"}},
        {"run_id": "x", "hold_started_at": "t", "request_counts": {"since": "t", "write_total": "many", "write_failed": 0}},
    ],
    ids=["incomplete-read", "non-object", "missing-counts", "non-numeric-counts"],
)
def test_worker_count_failure_never_fails_a_finished_switch_and_stops_the_prober(fast, world, monkeypatch, status):
    stopped = []

    class Prober:
        def start(self):
            pass

        def snapshot(self):
            return {}

        def stop(self):
            stopped.append(True)
            return {}

    monkeypatch.setattr("app.features.workflows.runners.prober_from_env", lambda: Prober())
    monkeypatch.setattr("app.features.workflows.runners._cutover_worker", lambda: _RaisingWorker(status))
    monkeypatch.setattr("app.features.workflows.runners.PROBE_SETTLE_S", 0.0)
    monkeypatch.setattr("app.features.workflows.runners.time.sleep", lambda _s: None)

    _, run = _approve_cutover(fast, [STABILIZED], world)

    assert run.state == WorkflowState.SUCCEEDED
    assert _outcome(run) == "switched"
    assert stopped == [True]
    assert "write_requests" not in run.stage(StageName.CUTOVER).metrics


def test_failing_final_report_does_not_fail_a_finished_switch(monkeypatch):
    from app.features.workflows import runners

    class Prober:
        def start(self):
            pass

        def snapshot(self):
            return {}

        def stop(self):
            return {}

    class Ctx:
        calls = 0

        def report(self, state, progress, metrics=None, message=None):
            pass

    monkeypatch.setattr(runners, "prober_from_env", lambda: Prober())
    monkeypatch.setattr(runners, "_cutover_worker", lambda: None)
    monkeypatch.setattr(runners, "_request_cutover", lambda *a: {"state": "running", "phase": "stabilizing"})
    monkeypatch.setattr(
        runners.ProbedContext, "finish", lambda self, extra=None: (_ for _ in ()).throw(ValueError("store down"))
    )

    job = runners.run_cutover(SimpleNamespace(run_id="mig-1"), Ctx(), probe_settle_s=0)

    assert job["phase"] == "stabilizing"


def test_post_switch_failure_resumed_through_the_api_can_still_be_recorded(fast, world, monkeypatch):
    from app.features.workflows.context import with_stage
    from app.features.workflows.runners import clean_up_cdc

    # 화면은 이 재개 버튼을 숨기지만 API 는 받습니다. 재개한 뒤에도 빠져나갈 길이 있어야 합니다.
    failed_after_switch = {"state": "failed", "phase": "cutover", "message": "목적지로 전환 후 오류"}
    run_id, run = _approve_cutover(fast, [failed_after_switch], world)
    assert _outcome(run) == "post_switch_failed"
    fast.resume(run_id)
    assert fast.advance(run_id).waiting_gate == "cutover"
    fast._store.update(run_id, lambda r: with_stage(r, StageName.DB_MIGRATION, metrics={"cdc_error": "끝남"}))
    assert cutover_readiness(fast.get(run_id)).decision == "no_go"
    calls_before = len(world.cutover_calls)

    # 트래픽이 목적지에 있는 동안 복제를 지우면 안 됩니다.
    monkeypatch.setattr(
        "app.features.workflows.runners._read_worker_status", lambda: {"mode": "TARGET", "next_origin": "TARGET"}
    )
    stopped = []
    monkeypatch.setattr("app.features.db_migration.service.stop", lambda run_id, agent=None: stopped.append(run_id))
    world.jobs = [failed_after_switch]
    with pytest.raises(RuntimeError):
        clean_up_cdc(fast.get(run_id))
    assert stopped == []

    fast.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}, note="전환 완료 확인"))
    run = fast.advance(run_id)

    assert run.state == WorkflowState.SUCCEEDED
    assert len(world.cutover_calls) == calls_before



def test_onprem_uses_main_cutover_after_both_approvals(engine, world, monkeypatch):
    from app.features.provisioning.onprem import service
    from app.features.workflows.schemas import TargetInfraConfig

    monkeypatch.setenv("ONPREM_CONFIG_REF", "secret://operator/onprem")

    def provision(**kwargs):
        world.order.append("provisioning")
        assert kwargs["config_ref"] == "secret://operator/onprem"
        return {"target": {"db": {"private_ip": "127.0.0.1", "port": 15432}}, "applied": True}

    def deploy(**kwargs):
        world.order.append("app_deploy")
        return {"app_url": "https://was.example.test", "healthy": True, "worker_target_updated": True}

    monkeypatch.setattr(service, "provision", provision)
    monkeypatch.setattr(service, "deploy_app", deploy)
    request = REQUEST.model_copy(update={"target_infra": TargetInfraConfig(provider="onprem")})
    run_id = engine.advance(engine.create_run(request).run_id).run_id
    engine.approve(run_id, "plan")
    run = engine.advance(run_id)
    assert run.waiting_gate == "cutover"
    assert world.order == ["planning", "provisioning", "db_migration", "app_deploy"]
    assert not world.cutover_calls
    engine.approve(run_id, "cutover", approver="test", acknowledge_unknown=True)
    run = engine.advance(run_id)
    assert world.cutover_calls == [(to_job_id(run_id), "test")]
    assert run.state == WorkflowState.SUCCEEDED
    assert run.stage(StageName.CUTOVER).metrics["cutover_sequence"] == "VERIFIED"
