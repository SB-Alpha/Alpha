import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.features.workflows.context import RunAborted, StoreRunContext, with_stage
from app.features.workflows.engine import Engine, InvalidTransition
from app.features.workflows.schemas import (
    ApprovalRecord,
    ApproveRequest,
    ManualStageInput,
    MigrationRequest,
    RunContext,
    RunStatus,
    StageName,
    WorkflowState,
)
from app.features.workflows.store import RunNotFound, RunStore

REQUEST = MigrationRequest(
    github_repo_url="https://github.com/example/target-app",
    aws_region="ap-northeast-2",
)


class FakePlan(BaseModel):
    summary: list[str]
    approvable: bool


def discovery_runner(run: RunStatus, ctx: RunContext) -> dict:
    ctx.report("running", 50, {"tables": 1}, "DB 조회")
    return {"db": {"engine": "postgres", "engine_version": "16.4"}}


def planning_runner(run: RunStatus, ctx: RunContext) -> FakePlan:
    # planning PR #21 처럼 마지막에 waiting_approval 로 보고해도 단계는 succeeded 로 끝나야 함
    assert run.outputs["discovery"]["db"]["engine"] == "postgres"
    ctx.report("waiting_approval", 100, None, "계획 생성 완료")
    return FakePlan(summary=["a", "b", "c"], approvable=True)


def ok_runner(run: RunStatus, ctx: RunContext) -> dict:
    ctx.report("running", 100)
    return {"ok": True}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "workflows.sqlite3"


def make_engine(db_path: Path, **runners) -> Engine:
    return Engine(RunStore(db_path), {StageName(k): v for k, v in runners.items()})


def test_unimplemented_stage_pauses_without_fake_success(db_path):
    engine = make_engine(db_path)
    run = engine.advance(engine.create_run(REQUEST).run_id)

    assert run.state == WorkflowState.PAUSED
    assert run.current_stage == StageName.DISCOVERY
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.PENDING
    assert "미구현" in run.stage(StageName.DISCOVERY).message
    assert all(s.state != WorkflowState.SUCCEEDED for s in run.stages)


def test_runs_stages_in_order_and_waits_for_plan_approval(db_path):
    engine = make_engine(db_path, discovery=discovery_runner, planning=planning_runner)
    run = engine.advance(engine.create_run(REQUEST).run_id)

    assert run.state == WorkflowState.WAITING_APPROVAL
    assert run.waiting_gate == "plan"
    assert run.current_stage == StageName.PROVISIONING
    assert run.stage(StageName.DISCOVERY).metrics == {"tables": 1}
    assert run.stage(StageName.PLANNING).state == WorkflowState.SUCCEEDED
    assert run.outputs["planning"] == {"summary": ["a", "b", "c"], "approvable": True}


def test_approval_passes_gate_then_stops_at_next_unimplemented_stage(db_path):
    engine = make_engine(db_path, discovery=discovery_runner, planning=planning_runner)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id

    approved = engine.approve(run_id, "plan", "확인함")
    assert approved.state == WorkflowState.RUNNING
    assert approved.approvals == ["plan"]

    run = engine.advance(run_id)
    assert run.state == WorkflowState.PAUSED
    assert run.current_stage == StageName.PROVISIONING


def test_approve_rejects_wrong_gate_or_state(db_path):
    engine = make_engine(db_path, discovery=discovery_runner, planning=planning_runner)
    run_id = engine.create_run(REQUEST).run_id

    with pytest.raises(InvalidTransition):
        engine.approve(run_id, "plan")  # 아직 승인 대기 아님

    engine.advance(run_id)
    with pytest.raises(InvalidTransition):
        engine.approve(run_id, "cutover")  # 기다리는 건 계획 승인


def test_full_flow_reaches_succeeded_after_two_approvals(db_path):
    engine = make_engine(
        db_path,
        discovery=discovery_runner,
        planning=planning_runner,
        provisioning=ok_runner,
        db_migration=ok_runner,
        app_deploy=ok_runner,
        cutover=ok_runner,
    )
    run_id = engine.create_run(REQUEST).run_id

    assert engine.advance(run_id).waiting_gate == "plan"
    engine.approve(run_id, "plan")
    assert engine.advance(run_id).waiting_gate == "cutover"
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    run = engine.advance(run_id)

    assert run.state == WorkflowState.SUCCEEDED
    assert run.current_stage is None
    assert all(s.state == WorkflowState.SUCCEEDED and not s.manual for s in run.stages)


def test_manual_input_is_marked_and_run_continues(db_path):
    engine = make_engine(db_path)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id

    engine.submit_manual(run_id, StageName.DISCOVERY, ManualStageInput(output={"x": 1}, note="fixture"))
    run = engine.advance(run_id)

    discovery = run.stage(StageName.DISCOVERY)
    assert discovery.state == WorkflowState.SUCCEEDED
    assert discovery.manual is True
    assert run.outputs["discovery"] == {"x": 1}
    assert run.state == WorkflowState.PAUSED
    assert run.current_stage == StageName.PLANNING


def test_manual_input_rejects_out_of_order_stage_and_ungated_stage(db_path):
    engine = make_engine(db_path)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id

    with pytest.raises(InvalidTransition):
        engine.submit_manual(run_id, StageName.PLANNING, ManualStageInput(output={}))

    engine.submit_manual(run_id, StageName.DISCOVERY, ManualStageInput(output={}))
    engine.advance(run_id)
    engine.submit_manual(run_id, StageName.PLANNING, ManualStageInput(output={}))
    run = engine.advance(run_id)
    assert run.state == WorkflowState.WAITING_APPROVAL
    with pytest.raises(InvalidTransition):  # 계획 승인 전에는 provisioning 결과를 넣을 수 없음
        engine.submit_manual(run_id, StageName.PROVISIONING, ManualStageInput(output={}))


def test_failing_stage_stops_run_and_manual_input_recovers(db_path):
    def broken(run: RunStatus, ctx: RunContext) -> dict:
        raise RuntimeError("AWS 조회 실패")

    engine = make_engine(db_path, discovery=broken)
    run = engine.advance(engine.create_run(REQUEST).run_id)

    assert run.state == WorkflowState.FAILED
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.FAILED
    assert "AWS 조회 실패" in run.stage(StageName.DISCOVERY).message

    engine.submit_manual(run.run_id, StageName.DISCOVERY, ManualStageInput(output={}))
    assert engine.advance(run.run_id).current_stage == StageName.PLANNING


def test_abort_stops_module_at_next_report(db_path):
    holder: dict = {}

    def long_running(run: RunStatus, ctx: RunContext) -> dict:
        holder["engine"].abort(run.run_id, "운영자 중단")
        ctx.report("running", 50)  # abort 이후 보고 → 여기서 멈춤
        raise AssertionError("abort 이후에도 계속 실행됨")

    engine = make_engine(db_path, discovery=long_running)
    holder["engine"] = engine
    run = engine.advance(engine.create_run(REQUEST).run_id)

    assert run.state == WorkflowState.ABORTED
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.ABORTED
    assert "discovery" not in run.outputs
    with pytest.raises(InvalidTransition):
        engine.abort(run.run_id)


def test_state_survives_restart(db_path):
    first = make_engine(db_path, discovery=discovery_runner, planning=planning_runner)
    run_id = first.advance(first.create_run(REQUEST).run_id).run_id

    restarted = make_engine(db_path)  # 새 프로세스처럼 저장소를 다시 엶
    run = restarted.get(run_id)
    assert run.state == WorkflowState.WAITING_APPROVAL
    assert run.outputs["planning"]["approvable"] is True
    assert [r.run_id for r in restarted.list()] == [run_id]


def test_report_rejects_invalid_values(db_path):
    store = RunStore(db_path)
    run_id = Engine(store).create_run(REQUEST).run_id
    ctx = StoreRunContext(store, run_id, StageName.DISCOVERY)

    with pytest.raises(ValueError):
        ctx.report("done", 10)
    with pytest.raises(ValueError):
        ctx.report("running", 101)
    # 타 모듈(assessment 등)의 succeeded 보고는 크래시 대신 running (progress=100)으로 안전하게 수용
    ctx.report("succeeded", 100)
    assert store.get(run_id).stage(StageName.DISCOVERY).progress == 100


def test_report_keeps_stage_running_so_it_cannot_be_skipped(db_path):
    def reports_waiting(run: RunStatus, ctx: RunContext) -> dict:
        ctx.report("waiting_approval", 90, None, "거의 끝")
        current = holder["engine"].get(run.run_id)
        assert current.stage(StageName.DISCOVERY).state == WorkflowState.RUNNING
        return {}

    holder: dict = {}
    engine = make_engine(db_path, discovery=reports_waiting)
    holder["engine"] = engine
    run = engine.advance(engine.create_run(REQUEST).run_id)
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED
    assert run.stage(StageName.DISCOVERY).message == "거의 끝"


def test_manual_input_rejected_while_registered_module_is_due(db_path):
    engine = make_engine(db_path, discovery=discovery_runner)
    run_id = engine.create_run(REQUEST).run_id  # pending: advance 전

    with pytest.raises(InvalidTransition):
        engine.submit_manual(run_id, StageName.DISCOVERY, ManualStageInput(output={}))


def test_resume_retries_failed_stage(db_path):
    calls = {"n": 0}

    def flaky(run: RunStatus, ctx: RunContext) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("일시 오류")
        return {"ok": True}

    engine = make_engine(db_path, discovery=flaky)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    assert engine.get(run_id).state == WorkflowState.FAILED

    resumed = engine.resume(run_id)
    assert resumed.stage(StageName.DISCOVERY).state == WorkflowState.PENDING
    run = engine.advance(run_id)
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.SUCCEEDED
    assert run.stage(StageName.DISCOVERY).manual is False
    assert run.current_stage == StageName.PLANNING
    with pytest.raises(InvalidTransition):  # paused 는 resume 대상 아님 (수동 입력으로)
        engine.resume(run_id)


@pytest.mark.parametrize("output", [["not", "a", "dict"], ["ok", "hi"]])
def test_non_mapping_output_fails_stage_instead_of_fake_success(db_path, output):
    def bad_output(run: RunStatus, ctx: RunContext):
        return output

    engine = make_engine(db_path, discovery=bad_output)
    run = engine.advance(engine.create_run(REQUEST).run_id)
    assert run.state == WorkflowState.FAILED
    assert run.stage(StageName.DISCOVERY).state == WorkflowState.FAILED
    assert "discovery" not in run.outputs


def test_fractional_progress_keeps_run_readable(db_path):
    store = RunStore(db_path)
    engine = Engine(store)
    run_id = engine.create_run(REQUEST).run_id

    StoreRunContext(store, run_id, StageName.DISCOVERY).report("running", 100 / 3)

    assert engine.get(run_id).stage(StageName.DISCOVERY).progress == 33
    assert [r.run_id for r in engine.list(10)] == [run_id]


def test_unknown_run_raises(db_path):
    with pytest.raises(RunNotFound):
        make_engine(db_path).get("mig-missing")


def test_report_after_abort_raises(db_path):
    store = RunStore(db_path)
    engine = Engine(store)
    run_id = engine.create_run(REQUEST).run_id
    engine.abort(run_id)
    with pytest.raises(RunAborted):
        StoreRunContext(store, run_id, StageName.DISCOVERY).report("running", 1)


# -- 승인 기록과 실패 후 재승인 ---------------------------------------------------


def _engine_at_cutover_gate(db_path: Path, cutover) -> tuple[Engine, str]:
    engine = make_engine(
        db_path,
        discovery=ok_runner,
        planning=ok_runner,
        provisioning=ok_runner,
        db_migration=ok_runner,
        app_deploy=ok_runner,
        cutover=cutover,
    )
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    engine.approve(run_id, "plan", approver="계획 승인자")
    run = engine.advance(run_id)
    assert (run.state, run.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    return engine, run_id


class CountingCutover:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def __call__(self, run: RunStatus, ctx: RunContext) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        return {"ok": True}


def test_approve_appends_record_and_keeps_existing_message(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)

    run = engine.approve(run_id, "cutover", "확인함", approver="박준서", preflight_verdict="no_go", acknowledge_unknown=True)

    assert run.message == "전환 승인 ② 완료: 확인함"
    assert run.approvals == ["plan", "cutover"]
    assert [r.gate for r in run.approval_records] == ["plan", "cutover"]
    record = run.approval_records[-1]
    assert (record.approver, record.comment, record.preflight_verdict) == ("박준서", "확인함", "no_go")
    assert record.at is not None


def test_approve_without_approver_records_none(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    record = engine.approve(run_id, "cutover", acknowledge_unknown=True).approval_records[-1]
    assert (record.approver, record.comment, record.preflight_verdict) == (None, None, None)


def test_cutover_approve_stores_the_reported_preflight_time(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    measured = datetime(2026, 11, 6, 1, 2, 3, tzinfo=timezone.utc)

    run = engine.approve(run_id, "cutover", preflight_verdict="unverified", preflight_measured_at=measured, acknowledge_unknown=True)

    record = engine.get(run_id).approval_records[-1]
    assert record == run.approval_records[-1]
    assert (record.preflight_verdict, record.preflight_measured_at) == ("unverified", measured)


def test_plan_approve_never_stores_preflight_values(db_path):
    engine = make_engine(db_path, discovery=ok_runner, planning=ok_runner)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    measured = datetime(2026, 11, 6, 1, 2, 3, tzinfo=timezone.utc)

    record = engine.approve(run_id, "plan", preflight_verdict="go", preflight_measured_at=measured).approval_records[-1]

    assert (record.gate, record.preflight_verdict, record.preflight_measured_at) == ("plan", None, None)


def test_approval_records_stored_without_measured_time_still_load():
    record = ApprovalRecord.model_validate({"gate": "cutover", "preflight_verdict": "no_go", "at": "2026-11-06T01:02:03Z"})
    assert (record.preflight_verdict, record.preflight_measured_at) == ("no_go", None)


def test_rejected_approve_leaves_no_record(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    with pytest.raises(InvalidTransition):
        engine.approve(run_id, "plan", approver="박준서")
    assert [r.gate for r in engine.get(run_id).approval_records] == ["plan"]


def test_rows_stored_without_approval_records_still_load(db_path):
    store = RunStore(db_path)
    run_id = Engine(store).create_run(REQUEST).run_id
    with sqlite3.connect(db_path) as conn:
        data = json.loads(conn.execute("SELECT data FROM runs").fetchone()[0])
        del data["approval_records"]
        conn.execute("UPDATE runs SET data = ?", (json.dumps(data),))

    assert store.get(run_id).approval_records == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("   \t", None), (" " * 101, None), (" 박준서 ", "박준서"), ("x" * 100, "x" * 100)],
)
def test_approve_request_normalizes_blank_approver(raw, expected):
    assert ApproveRequest(gate="cutover", approver=raw).approver == expected


def test_approve_request_rejects_long_approver():
    with pytest.raises(ValidationError):
        ApproveRequest(gate="cutover", approver="x" * 101)


@pytest.mark.parametrize(
    "error", [RuntimeError("컷오버 실패로 원본으로 롤백되었습니다: x"), TimeoutError("대기 시간 초과")]
)
def test_failed_cutover_needs_fresh_approval_on_resume(db_path, error):
    cutover = CountingCutover(error)
    engine, run_id = _engine_at_cutover_gate(db_path, cutover)
    engine.approve(run_id, "cutover", approver="박준서", acknowledge_unknown=True)

    failed = engine.advance(run_id)

    assert failed.state == WorkflowState.FAILED
    assert failed.approvals == ["plan"]
    assert [r.gate for r in failed.approval_records] == ["plan", "cutover"]
    assert "전환 승인 ②" in failed.message
    # 진단이 읽는 단계 오류 메시지는 그대로입니다
    assert failed.stage(StageName.CUTOVER).message == f"{type(error).__name__}: {error}"
    assert cutover.calls == 1

    engine.resume(run_id)
    waiting = engine.advance(run_id)

    assert (waiting.state, waiting.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    assert waiting.current_stage == StageName.CUTOVER
    assert cutover.calls == 1

    cutover.error = None
    engine.approve(run_id, "cutover", approver="이주호", acknowledge_unknown=True)
    done = engine.advance(run_id)

    assert done.state == WorkflowState.SUCCEEDED
    assert cutover.calls == 2
    assert [r.approver for r in done.approval_records if r.gate == "cutover"] == ["박준서", "이주호"]


def test_stuck_cutover_stage_also_drops_the_approval(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    store = engine._store
    store.update(run_id, lambda r: with_stage(r, StageName.CUTOVER, state=WorkflowState.RUNNING))

    engine._mark_failed(run_id, None, "workflows 내부 오류")

    run = engine.get(run_id)
    assert run.state == WorkflowState.FAILED
    assert run.approvals == ["plan"]
    assert run.message.startswith("workflows 내부 오류")
    assert "전환 승인 ②" in run.message


def _legacy_failed_cutover_row(db_path: Path, engine: Engine, run_id: str) -> None:
    """이 변경 전에 저장된 row: 전환 실패인데 승인 ② 가 남아 있고 approval_records·cutover_outcome 이 없습니다."""
    engine._store.update(
        run_id,
        lambda r: with_stage(r, StageName.CUTOVER, state=WorkflowState.FAILED, message="RuntimeError: 실패").model_copy(
            update={"state": WorkflowState.FAILED, "waiting_gate": None, "approvals": ["plan", "cutover"]}
        ),
    )
    with sqlite3.connect(db_path) as conn:
        data = json.loads(conn.execute("SELECT data FROM runs").fetchone()[0])
        del data["approval_records"]
        conn.execute("UPDATE runs SET data = ?", (json.dumps(data),))


def test_resuming_a_legacy_row_does_not_reuse_its_stale_approval(db_path):
    cutover = CountingCutover()
    engine, run_id = _engine_at_cutover_gate(db_path, cutover)
    _legacy_failed_cutover_row(db_path, engine, run_id)

    engine.resume(run_id)
    run = engine.advance(run_id)

    assert (run.state, run.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    assert cutover.calls == 0
    assert run.approvals == ["plan"]
    assert run.approval_records == []
    # 이전 결과를 모르므로 화면이 "전환 전" 으로 말하지 않게 합니다.
    assert run.stage(StageName.CUTOVER).metrics["cutover_outcome"] == "unknown"


def test_resume_of_an_earlier_failed_stage_leaves_cutover_untouched(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    engine._store.update(
        run_id,
        lambda r: with_stage(r, StageName.APP_DEPLOY, state=WorkflowState.FAILED).model_copy(
            update={"state": WorkflowState.FAILED, "waiting_gate": None}
        ),
    )

    run = engine.resume(run_id)

    assert run.approvals == ["plan"]
    assert "cutover_outcome" not in run.stage(StageName.CUTOVER).metrics


class OutcomeCutover(CountingCutover):
    """결과를 보고한 뒤 실패하는 runner 입니다. 실제 run_cutover 의 경계만 흉내 냅니다."""

    def __init__(self, outcome: str) -> None:
        super().__init__(RuntimeError("실패"))
        self.outcome = outcome

    def __call__(self, run: RunStatus, ctx: RunContext) -> dict:
        ctx.report("running", 50, {"cutover_outcome": self.outcome})
        return super().__call__(run, ctx)


@pytest.mark.parametrize("outcome", ["rolled_back", "not_started"])
def test_failure_confirmed_on_source_revokes_the_approval(db_path, outcome):
    engine, run_id = _engine_at_cutover_gate(db_path, OutcomeCutover(outcome))
    engine.approve(run_id, "cutover", acknowledge_unknown=True)

    run = engine.advance(run_id)

    assert run.approvals == ["plan"]
    assert "전환 승인 ②" in run.message


@pytest.mark.parametrize("outcome", ["post_switch_failed", "unknown"])
def test_failure_that_may_have_moved_traffic_keeps_the_manual_path(db_path, outcome):
    engine, run_id = _engine_at_cutover_gate(db_path, OutcomeCutover(outcome))
    engine.approve(run_id, "cutover", acknowledge_unknown=True)

    run = engine.advance(run_id)

    assert run.state == WorkflowState.FAILED
    assert run.approvals == ["plan", "cutover"]
    assert "수동 확인 필요" in run.message
    assert "전환 승인 ②" not in run.message

    done = engine.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"checked": True}))
    assert done.stage(StageName.CUTOVER).manual is True


@pytest.mark.parametrize("outcome", ["post_switch_failed", "unknown"])
def test_resume_after_uncertain_outcome_needs_approval_and_keeps_the_warning(db_path, outcome):
    cutover = OutcomeCutover(outcome)
    engine, run_id = _engine_at_cutover_gate(db_path, cutover)
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    engine.advance(run_id)

    engine.resume(run_id)
    run = engine.advance(run_id)

    assert (run.state, run.waiting_gate) == (WorkflowState.WAITING_APPROVAL, "cutover")
    assert cutover.calls == 1
    assert run.approvals == ["plan"]
    assert run.stage(StageName.CUTOVER).metrics["cutover_outcome"] == outcome


def test_internal_failure_after_the_switch_is_not_offered_as_a_retry(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    engine._store.update(
        run_id,
        lambda r: with_stage(r, StageName.CUTOVER, state=WorkflowState.RUNNING, metrics={"cutover_outcome": "switched"}),
    )

    engine._mark_failed(run_id, None, "workflows 내부 오류")

    run = engine.get(run_id)
    assert run.approvals == ["plan", "cutover"]
    assert "수동 확인 필요" in run.message


def test_internal_failure_without_stuck_cutover_keeps_the_approval(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    engine.approve(run_id, "cutover", acknowledge_unknown=True)

    engine._mark_failed(run_id, None, "workflows 내부 오류")

    run = engine.get(run_id)
    assert run.approvals == ["plan", "cutover"]
    assert run.message == "workflows 내부 오류"


def test_failed_provisioning_keeps_plan_approval_and_retries_without_approval(db_path):
    calls = []

    def flaky(run: RunStatus, ctx: RunContext) -> dict:
        calls.append(run.run_id)
        if len(calls) == 1:
            raise RuntimeError("terraform 오류")
        return {"ok": True}

    engine = make_engine(db_path, discovery=ok_runner, planning=ok_runner, provisioning=flaky)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    engine.approve(run_id, "plan")

    failed = engine.advance(run_id)
    assert failed.state == WorkflowState.FAILED
    assert failed.approvals == ["plan"]
    assert failed.message == "provisioning 실패"

    engine.resume(run_id)
    run = engine.advance(run_id)
    assert len(calls) == 2
    assert run.waiting_gate is None
    assert run.stage(StageName.PROVISIONING).state == WorkflowState.SUCCEEDED


def test_manual_input_cannot_bypass_fresh_approval_after_failed_cutover(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, CountingCutover(RuntimeError("롤백")))
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    assert engine.advance(run_id).state == WorkflowState.FAILED

    with pytest.raises(InvalidTransition, match="전환 승인"):
        engine.submit_manual(run_id, StageName.CUTOVER, ManualStageInput(output={"done": True}))

    assert engine.get(run_id).state == WorkflowState.FAILED


def test_aborted_run_is_untouched_by_late_cutover_failure(db_path):
    engine, run_id = _engine_at_cutover_gate(db_path, ok_runner)
    engine.approve(run_id, "cutover", acknowledge_unknown=True)
    engine.abort(run_id, "운영자 중단")

    engine._mark_failed(run_id, StageName.CUTOVER, "늦은 실패")

    run = engine.get(run_id)
    assert run.state == WorkflowState.ABORTED
    assert run.approvals == ["plan", "cutover"]
def _aborted_run_with_cleanup(db_path, cleanup):
    engine = Engine(RunStore(db_path), abort_cleanup=cleanup)
    run_id = engine.create_run(REQUEST).run_id
    return engine, run_id, engine.abort(run_id, "테스트")


def test_abort_marks_cleanup_running_then_done(db_path):
    seen = []

    def cleanup(run: RunStatus) -> bool:
        seen.append(run.state)
        return True

    engine, run_id, aborted = _aborted_run_with_cleanup(db_path, cleanup)
    assert aborted.cleanup_state == "running"

    engine.run_abort_cleanup(run_id)

    after = engine.get(run_id)
    assert seen == [WorkflowState.ABORTED]
    assert after.cleanup_state == "done"
    assert "완료" in after.cleanup_message
    assert after.updated_at == aborted.updated_at


def test_abort_cleanup_with_nothing_to_clean_leaves_no_note(db_path):
    engine, run_id, _ = _aborted_run_with_cleanup(db_path, lambda run: False)

    engine.run_abort_cleanup(run_id)

    after = engine.get(run_id)
    assert after.cleanup_state is None
    assert after.cleanup_message is None


def test_failed_abort_cleanup_is_recorded_and_can_be_retried(db_path):
    attempts = []

    def cleanup(run: RunStatus) -> bool:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("커넥터 서버 응답 없음")
        return True

    engine, run_id, _ = _aborted_run_with_cleanup(db_path, cleanup)

    engine.run_abort_cleanup(run_id)
    failed = engine.get(run_id)
    assert failed.cleanup_state == "failed"
    assert "커넥터 서버 응답 없음" in failed.cleanup_message

    assert engine.retry_cleanup(run_id).cleanup_state == "running"
    engine.run_abort_cleanup(run_id)
    assert engine.get(run_id).cleanup_state == "done"
    assert len(attempts) == 2


def test_cleanup_failure_scrubs_sensitive_details(db_path):
    def cleanup(run: RunStatus) -> bool:
        raise RuntimeError("failed to connect postgres://admin:supersecret@10.0.1.2:5432/db")

    engine, run_id, _ = _aborted_run_with_cleanup(db_path, cleanup)
    engine.run_abort_cleanup(run_id)
    failed = engine.get(run_id)
    assert failed.cleanup_state == "failed"
    assert "supersecret" not in failed.cleanup_message
    assert "10.0.1.2" not in failed.cleanup_message
    assert "[숨김]" in failed.cleanup_message


def test_cleanup_retry_is_refused_unless_it_failed(db_path):
    engine, run_id, _ = _aborted_run_with_cleanup(db_path, lambda run: True)
    with pytest.raises(InvalidTransition):
        engine.retry_cleanup(run_id)  # 아직 정리 중
    engine.run_abort_cleanup(run_id)
    with pytest.raises(InvalidTransition):
        engine.retry_cleanup(run_id)  # 이미 끝남


def test_abort_without_cleanup_configured_stays_as_before(db_path):
    engine = make_engine(db_path)
    run_id = engine.create_run(REQUEST).run_id

    assert engine.abort(run_id).cleanup_state is None
    engine.run_abort_cleanup(run_id)
    assert engine.get(run_id).cleanup_state is None


def test_report_after_success_updates_metrics_without_reopening_the_stage(db_path):
    store = RunStore(db_path)
    engine = Engine(store, {StageName.DISCOVERY: discovery_runner})
    run_id = engine.create_run(REQUEST).run_id
    before = engine.advance(run_id)

    StoreRunContext(store, run_id, StageName.DISCOVERY).report("running", 7, {"lag": 3}, "복제 중")

    after = engine.get(run_id)
    stage = after.stage(StageName.DISCOVERY)
    assert stage.state == WorkflowState.SUCCEEDED
    assert stage.progress == 100
    assert stage.metrics == {"tables": 1, "lag": 3}
    assert stage.message == "복제 중"
    assert after.state == before.state
    assert after.updated_at == before.updated_at


def test_report_after_failure_keeps_the_error_and_allows_resume(db_path):
    def fails(run: RunStatus, ctx: RunContext) -> dict:
        raise RuntimeError("원래 오류")

    store = RunStore(db_path)
    engine = Engine(store, {StageName.DISCOVERY: fails})
    run_id = engine.create_run(REQUEST).run_id
    before = engine.advance(run_id)
    failed_message = before.stage(StageName.DISCOVERY).message

    StoreRunContext(store, run_id, StageName.DISCOVERY).report("running", 7, {"lag": 3}, "복제 중")

    after = engine.get(run_id)
    stage = after.stage(StageName.DISCOVERY)
    assert stage.state == WorkflowState.FAILED
    assert stage.message == failed_message
    assert stage.metrics == {"lag": 3}
    assert after.updated_at == before.updated_at
    assert engine.resume(run_id).state == WorkflowState.RUNNING
