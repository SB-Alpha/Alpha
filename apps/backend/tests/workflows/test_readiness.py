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


# s5 (mig-20261107-a333af6e): 승인 순간 실제 지연은 48,555,488 bytes 였는데, 30초 전 400 bytes 값으로 "가능"이 나왔습니다.
MEASURED = datetime(2026, 11, 7, 17, 0, 0, tzinfo=timezone.utc)


def _lag_at(lag: int, measured_at: str | None):
    metrics = {**GOOD_METRICS, "replication_lag_bytes": lag}
    if measured_at is not None:
        metrics["measured_at"] = measured_at
    return make_run(metrics)


def test_a_stale_lag_value_is_not_counted_as_pass(monkeypatch):
    from datetime import timedelta

    from app.features.workflows.readiness import max_lag_age_s

    monkeypatch.delenv("CDC_AGENT_METRICS_INTERVAL_S", raising=False)
    assert max_lag_age_s() == 20  # 에이전트 주기 10초의 두 번
    run = _lag_at(400, MEASURED.isoformat())
    stale = cutover_readiness(run, now=MEASURED + timedelta(seconds=30))
    lag = next(c for c in stale.checks if c.id == "lag")
    assert lag.status == "unknown"
    assert "30초 전" in lag.detail
    assert stale.decision == "needs_confirmation"

    fresh = cutover_readiness(run, now=MEASURED + timedelta(seconds=max_lag_age_s() - 1))
    assert next(c for c in fresh.checks if c.id == "lag").status == "pass"
    assert fresh.decision == "go"


def test_a_lag_over_the_limit_still_fails_even_when_stale():
    from datetime import timedelta

    run = _lag_at(48_555_488, MEASURED.isoformat())
    readiness = cutover_readiness(run, now=MEASURED + timedelta(seconds=120))
    assert next(c for c in readiness.checks if c.id == "lag").status == "fail"
    assert readiness.decision == "no_go"


@pytest.mark.parametrize("measured_at", [None, "not-a-time", "2026-11-07T17:00:00"])
def test_lag_without_a_usable_measured_at_keeps_the_value_check(measured_at):
    readiness = cutover_readiness(_lag_at(400, measured_at), now=MEASURED)
    lag = next(c for c in readiness.checks if c.id == "lag")
    assert lag.status == "pass"
    assert "측정 시각 미제공" in lag.detail


def test_measured_at_from_another_stage_is_not_used_for_the_lag():
    from datetime import timedelta

    run = _lag_at(400, None)
    run = with_stage(run, StageName.PROVISIONING, metrics={"measured_at": MEASURED.isoformat()})
    lag = next(c for c in cutover_readiness(run, now=MEASURED + timedelta(seconds=300)).checks if c.id == "lag")
    assert lag.status == "pass"


def test_the_stale_limit_follows_the_agent_metrics_interval(monkeypatch):
    """에이전트가 30초마다 재는 환경은 같은 값을 넣어 판정이 30초 주기로 흔들리지 않게 합니다 (기준 60초)."""
    from datetime import timedelta

    from app.features.workflows.readiness import max_lag_age_s

    monkeypatch.setenv("CDC_AGENT_METRICS_INTERVAL_S", "30")
    assert max_lag_age_s() == 60
    run = _lag_at(400, MEASURED.isoformat())
    # softwareDefine 실측: measured_at 이 31초 간격으로 바뀜. 기준 60초면 그 사이에 확인 중으로 바뀌지 않음
    assert next(c for c in cutover_readiness(run, now=MEASURED + timedelta(seconds=31)).checks if c.id == "lag").status == "pass"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan"])
def test_an_unusable_agent_interval_falls_back_to_the_default(monkeypatch, bad):
    from app.features.workflows.readiness import max_lag_age_s

    monkeypatch.setenv("CDC_AGENT_METRICS_INTERVAL_S", bad)
    assert max_lag_age_s() == 20
