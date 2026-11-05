from pathlib import Path

import pytest
from pydantic import BaseModel

from app.features.workflows.context import RunAborted, StoreRunContext
from app.features.workflows.engine import Engine, InvalidTransition
from app.features.workflows.schemas import (
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
    engine.approve(run_id, "cutover")
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
