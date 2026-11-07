from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.features.workflows.context import with_stage
from app.features.workflows.engine import Engine, InvalidTransition
from app.features.workflows.readiness import MAX_REPLICATION_LAG_BYTES, cutover_readiness
from app.features.workflows.schemas import (
    MigrationRequest,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)
from app.features.workflows.store import RunStore

REQUEST = MigrationRequest(github_repo_url="https://github.com/example/target-app", aws_region="ap-northeast-2")
GOOD_METRICS = {"phase": "cutover_ready", "copy_percent": 100, "replication_lag_bytes": 0, "mismatch": 0}


def make_run(metrics: dict | None = None, *, healthy: bool | None = True, manual_app: bool = False) -> RunStatus:
    now = datetime.now(timezone.utc)
    run = RunStatus(
        run_id="mig-readiness",
        state=WorkflowState.WAITING_APPROVAL,
        current_stage=StageName.CUTOVER,
        waiting_gate="cutover",
        approvals=["plan"],
        request=REQUEST,
        stages=[StageStatus(stage=s) for s in StageName],
        outputs={} if healthy is None else {"app_deploy": {"healthy": healthy}},
        created_at=now,
        updated_at=now,
    )
    run = with_stage(run, StageName.DB_MIGRATION, metrics=GOOD_METRICS if metrics is None else metrics)
    return with_stage(run, StageName.APP_DEPLOY, manual=manual_app)


def check(run: RunStatus, check_id: str):
    return next(c for c in cutover_readiness(run).checks if c.id == check_id)


def test_all_passing_is_go():
    readiness = cutover_readiness(make_run())
    assert readiness.decision == "go"
    assert {c.status for c in readiness.checks} == {"pass"}


@pytest.mark.parametrize(
    ("change", "check_id"),
    [
        ({"phase": "streaming", "copy_percent": 99}, "copy"),
        ({"replication_lag_bytes": MAX_REPLICATION_LAG_BYTES + 1}, "lag"),
        ({"phase": "streaming", "mismatch": 1}, "mismatch"),
        ({"cdc_error": "커넥터 실패"}, "cdc"),
    ],
)
def test_a_failing_metric_is_no_go(change, check_id):
    run = make_run({**GOOD_METRICS, **change})
    assert cutover_readiness(run).decision == "no_go"
    assert check(run, check_id).status == "fail"


def test_brief_mismatch_during_live_writes_does_not_block_a_verified_job():
    # 쓰기 중에는 두 DB 를 다른 순간에 읽어 값이 오간다 (10 rps 로컬 리허설에서 0·1 반복을 실측).
    run = make_run({**GOOD_METRICS, "mismatch": 1})
    result = check(run, "mismatch")
    assert result.status == "pass"
    assert "기준점" in result.detail
    assert cutover_readiness(run).decision == "go"


def test_copy_percent_dipping_during_live_writes_does_not_block_a_verified_job():
    # 원본에 행이 먼저 늘어 복사율이 순간 99% 가 된다 (10 rps 로컬 리허설에서 실측).
    run = make_run({**GOOD_METRICS, "copy_percent": 99})
    assert check(run, "copy").status == "pass"
    assert "99%" in check(run, "copy").detail
    assert cutover_readiness(run).decision == "go"


def test_cdc_error_detail_carries_the_reason():
    assert "커넥터 실패" in check(make_run({**GOOD_METRICS, "cdc_error": "커넥터 실패"}), "cdc").detail


def test_lag_exactly_at_the_limit_passes():
    run = make_run({**GOOD_METRICS, "replication_lag_bytes": MAX_REPLICATION_LAG_BYTES})
    assert check(run, "lag").status == "pass"


@pytest.mark.parametrize("missing", ["phase", "copy_percent", "replication_lag_bytes", "mismatch"])
def test_a_missing_metric_needs_confirmation(missing):
    run = make_run({k: v for k, v in GOOD_METRICS.items() if k != missing})
    assert cutover_readiness(run).decision == "needs_confirmation"


@pytest.mark.parametrize("value", [True, "많음", float("nan"), None])
def test_a_value_that_is_not_a_number_is_unknown(value):
    assert check(make_run({**GOOD_METRICS, "mismatch": value}), "mismatch").status == "unknown"


def test_a_numeric_string_counts_as_a_number():
    assert check(make_run({**GOOD_METRICS, "mismatch": "0"}), "mismatch").status == "pass"


def test_the_latest_stage_report_wins():
    run = make_run({**GOOD_METRICS, "phase": "streaming", "mismatch": 2})
    assert check(run, "mismatch").status == "fail"
    assert check(with_stage(run, StageName.APP_DEPLOY, metrics={"mismatch": 0}), "mismatch").status == "pass"


def test_metrics_from_a_past_cutover_attempt_are_ignored():
    run = make_run({**GOOD_METRICS, "phase": "streaming", "mismatch": 2})
    assert check(with_stage(run, StageName.CUTOVER, metrics={"mismatch": 0}), "mismatch").status == "fail"


def test_app_health_follows_the_deploy_result():
    assert check(make_run(healthy=False), "app_health").status == "fail"
    assert check(make_run(healthy=None), "app_health").status == "unknown"


def test_manual_app_deploy_is_never_counted_as_checked():
    run = make_run(healthy=True, manual_app=True)
    assert check(run, "app_health").status == "unknown"
    assert cutover_readiness(run).decision == "needs_confirmation"


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "workflows.sqlite3")


def test_approve_accepts_go_without_acknowledgement(store):
    run = store.create(make_run())
    assert Engine(store).approve(run.run_id, "cutover").state == WorkflowState.RUNNING


def test_approve_refuses_unknown_items_until_acknowledged(store):
    run = store.create(make_run(healthy=None))
    engine = Engine(store)

    with pytest.raises(InvalidTransition, match="목적지 앱 상태"):
        engine.approve(run.run_id, "cutover")
    assert engine.get(run.run_id).state == WorkflowState.WAITING_APPROVAL

    assert engine.approve(run.run_id, "cutover", acknowledge_unknown=True).state == WorkflowState.RUNNING


def test_approve_refuses_failures_even_when_acknowledged(store):
    run = store.create(make_run({**GOOD_METRICS, "phase": "streaming", "mismatch": 1}))
    engine = Engine(store)

    with pytest.raises(InvalidTransition, match="불일치 테이블"):
        engine.approve(run.run_id, "cutover", acknowledge_unknown=True)
    assert engine.get(run.run_id).waiting_gate == "cutover"


def test_plan_approval_is_not_gated_by_cutover_checks(store):
    run = store.create(make_run({"mismatch": 9}).model_copy(update={"waiting_gate": "plan", "approvals": []}))
    assert Engine(store).approve(run.run_id, "plan").state == WorkflowState.RUNNING
