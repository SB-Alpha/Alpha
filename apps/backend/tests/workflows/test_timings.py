from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.features.workflows.engine import Engine
from app.features.workflows.router import get_engine
from app.features.workflows.schemas import (
    ApprovalRecord,
    MigrationRequest,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from app.features.workflows.store import RunStore
from app.features.workflows.timings import run_timings, timing_baseline
from app.main import app

T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
REQ = MigrationRequest(github_repo_url="https://github.com/example/target-app", aws_region="ap-northeast-2")


def at(sec):
    return T0 + timedelta(seconds=sec)


def _run(done: dict[StageName, float], approvals: dict[str, float] | None = None, *, run_id="mig-t", state=None,
         manual=(), metrics=None) -> RunStatus:
    stages = []
    for name in StageName:
        if name in done:
            stages.append(StageStatus(stage=name, state=WorkflowState.SUCCEEDED, updated_at=at(done[name]),
                                      manual=name in manual, metrics=(metrics or {}).get(name, {})))
        else:
            stages.append(StageStatus(stage=name))
    records = [ApprovalRecord(gate=g, at=at(t)) for g, t in (approvals or {}).items()]
    ok = len(done) == len(StageName)
    return RunStatus(run_id=run_id, state=state or (WorkflowState.SUCCEEDED if ok else WorkflowState.RUNNING),
                     current_stage=None, request=REQ, stages=stages, approval_records=records,
                     created_at=at(0), updated_at=at(max(done.values(), default=0)))


FULL = {StageName.DISCOVERY: 3, StageName.PLANNING: 5, StageName.PROVISIONING: 105, StageName.DB_MIGRATION: 135,
        StageName.APP_DEPLOY: 185, StageName.CUTOVER: 196}
APPROVALS = {"plan": 20, "cutover": 190}


def secs(t):
    return {x.stage.value: x.seconds for x in t.stages}


def test_each_stage_is_measured_from_the_previous_finish():
    t = run_timings(_run(FULL, APPROVALS))
    assert secs(t) == {"discovery": 3.0, "planning": 2.0, "provisioning": 85.0, "db_migration": 30.0,
                       "app_deploy": 50.0, "cutover": 6.0}


def test_human_wait_at_gates_is_not_counted_as_stage_time_but_reported():
    t = run_timings(_run(FULL, APPROVALS))
    # 계획 승인 기다림 5→20 = 15초, 전환 승인 기다림 185→190 = 5초
    assert t.approval_wait_seconds == 20.0
    # 승인 ①·② 앞 단계는 승인 시각부터 잼: provisioning 20→105, cutover 190→196
    assert t.measured_total_seconds == 176.0


def test_unfinished_stages_have_no_value_and_a_reason():
    t = run_timings(_run({StageName.DISCOVERY: 3, StageName.PLANNING: 5}))
    assert secs(t)["discovery"] == 3.0 and secs(t)["provisioning"] is None
    assert next(x for x in t.stages if x.stage == StageName.PROVISIONING).note == "아직 성공하지 않음"


def test_manual_stage_is_not_measured():
    t = run_timings(_run(FULL, APPROVALS, manual=(StageName.DISCOVERY,)))
    d = next(x for x in t.stages if x.stage == StageName.DISCOVERY)
    assert d.seconds is None and "수동" in d.note
    assert secs(t)["planning"] == 2.0  # 다음 단계는 앞 단계 끝 시각을 그대로 씀


def test_out_of_order_timestamps_are_not_invented():
    run = _run({**FULL, StageName.PLANNING: 1})  # 3초에 끝난 discovery 뒤인데 1초
    t = run_timings(run)
    assert next(x for x in t.stages if x.stage == StageName.PLANNING).seconds is None


def test_missing_approval_record_does_not_break_it():
    t = run_timings(_run(FULL, {}))
    assert secs(t)["provisioning"] == 100.0 and t.approval_wait_seconds is None


def test_baseline_uses_only_real_successful_runs():
    runs = [
        _run(FULL, APPROVALS, run_id="a"),
        _run({k: v * 2 for k, v in FULL.items()}, {"plan": 40, "cutover": 380}, run_id="b"),
        _run(FULL, APPROVALS, run_id="sim", metrics={StageName.DB_MIGRATION: {"simulated": True}}),
        _run(FULL, APPROVALS, run_id="manual", manual=(StageName.PLANNING,)),
        _run(FULL, APPROVALS, run_id="failed", state=WorkflowState.FAILED),
        _run({StageName.DISCOVERY: 3}, run_id="partial"),
    ]
    b = timing_baseline(runs)
    assert b.runs_considered == 2
    db = next(s for s in b.stages if s.stage == StageName.DB_MIGRATION)
    assert (db.samples, db.min_seconds, db.median_seconds, db.max_seconds) == (2, 30.0, 45.0, 60.0)


def test_baseline_with_no_data_has_no_numbers():
    b = timing_baseline([])
    assert b.runs_considered == 0
    assert all(s.samples == 0 and s.median_seconds is None for s in b.stages)


@pytest.fixture
def client(tmp_path):
    engine = Engine(RunStore(tmp_path / "workflows.sqlite3"))
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()


def test_api_timings_and_baseline(client):
    c, engine = client
    run = engine.create_run(REQ)
    body = c.get(f"/runs/{run.run_id}/timings").json()
    assert body["run_id"] == run.run_id and body["measured_total_seconds"] is None
    assert c.get("/runs/mig-nope/timings").status_code == 404
    base = c.get("/runs/stats/timings")  # /{run_id}/timings 에 가려지지 않음
    assert base.status_code == 200 and base.json()["runs_considered"] == 0
