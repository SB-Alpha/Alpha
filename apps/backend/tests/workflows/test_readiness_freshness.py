"""복제 추적이 멈춰 오래된 값만 남았을 때, 그 값으로 통과시키지 않는다 (전환 승인 ②)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.features.workflows.engine import Engine, InvalidTransition
from app.features.workflows.readiness import MAX_METRIC_AGE_S, cutover_readiness
from app.features.workflows.schemas import RunStatus, StageName, WorkflowState
from app.features.workflows.store import RunStore

from .test_readiness import GOOD_METRICS, make_run

TRACKED = ["cdc", "copy", "lag", "mismatch"]


def aged(run: RunStatus, seconds: float | None) -> RunStatus:
    """db_migration 단계의 마지막 보고 시각을 seconds 초 전으로 되돌린다 (None 이면 시각 없음)."""
    when = None if seconds is None else datetime.now(timezone.utc) - timedelta(seconds=seconds)
    stages = [s.model_copy(update={"updated_at": when}) if s.stage == StageName.DB_MIGRATION else s for s in run.stages]
    return run.model_copy(update={"stages": stages})


def status(run: RunStatus) -> dict[str, str]:
    return {c.id: c.status for c in cutover_readiness(run).checks}


def test_a_fresh_report_stays_go():
    readiness = cutover_readiness(aged(make_run(), 5))
    assert readiness.decision == "go"


def test_the_limit_itself_still_counts_as_fresh():
    assert cutover_readiness(aged(make_run(), MAX_METRIC_AGE_S - 1)).decision == "go"


def test_an_old_report_is_not_counted_as_passing():
    readiness = cutover_readiness(aged(make_run(), MAX_METRIC_AGE_S + 30))
    assert {c.id: c.status for c in readiness.checks if c.id in TRACKED} == {i: "unknown" for i in TRACKED}
    assert readiness.decision == "needs_confirmation"
    assert all("확인 중" in c.detail and "초 전" in c.detail for c in readiness.checks if c.id in TRACKED)


def test_the_old_values_stay_visible_in_the_detail():
    detail = next(c.detail for c in cutover_readiness(aged(make_run(), 600)).checks if c.id == "lag")
    assert "0 bytes" in detail  # 마지막으로 본 값은 지우지 않고, 오래됐다는 표시만 앞에 붙인다


def test_the_app_health_does_not_depend_on_the_tracker():
    assert status(aged(make_run(), 600))["app_health"] == "pass"


def test_an_old_failure_stays_a_failure():
    run = aged(make_run({**GOOD_METRICS, "cdc_error": "커넥터 실패", "replication_lag_bytes": 10**9}), 600)
    result = status(run)
    assert result["cdc"] == "fail" and result["lag"] == "fail"
    assert cutover_readiness(run).decision == "no_go"


def test_a_missing_report_time_is_not_counted_as_passing():
    assert status(aged(make_run(), None))["lag"] == "unknown"


def test_a_report_time_without_timezone_is_read_as_utc():
    run = make_run()
    naive = (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(tzinfo=None)
    stages = [s.model_copy(update={"updated_at": naive}) if s.stage == StageName.DB_MIGRATION else s for s in run.stages]
    assert cutover_readiness(run.model_copy(update={"stages": stages})).decision == "go"


def test_a_clock_slightly_behind_the_report_is_not_stale():
    future = datetime.now(timezone.utc) + timedelta(seconds=3)
    run = make_run()
    stages = [s.model_copy(update={"updated_at": future}) if s.stage == StageName.DB_MIGRATION else s for s in run.stages]
    assert cutover_readiness(run.model_copy(update={"stages": stages})).decision == "go"


def test_the_limit_can_be_set_from_the_environment(monkeypatch):
    run = aged(make_run(), 100)
    assert cutover_readiness(run).decision == "needs_confirmation"
    monkeypatch.setenv("ALPHA_READINESS_MAX_METRIC_AGE_S", "300")
    assert cutover_readiness(run).decision == "go"


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "nan", "inf"])
def test_a_bad_limit_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("ALPHA_READINESS_MAX_METRIC_AGE_S", bad)
    assert cutover_readiness(aged(make_run(), 5)).decision == "go"
    assert cutover_readiness(aged(make_run(), MAX_METRIC_AGE_S + 30)).decision == "needs_confirmation"


def test_the_time_can_be_fixed_by_the_caller():
    run = make_run()
    later = datetime.now(timezone.utc) + timedelta(seconds=MAX_METRIC_AGE_S + 30)
    assert cutover_readiness(run).decision == "go"
    assert cutover_readiness(run, at=later).decision == "needs_confirmation"


# --- 승인 API: 오래된 값으로는 확인 없이 승인되지 않는다 ---------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "workflows.sqlite3")


def test_approval_needs_acknowledgement_when_the_tracker_has_gone_quiet(store):
    run = store.create(aged(make_run(), 600))
    engine = Engine(store)
    with pytest.raises(InvalidTransition, match="복제 지연"):
        engine.approve(run.run_id, "cutover")
    assert engine.get(run.run_id).state == WorkflowState.WAITING_APPROVAL
    assert engine.approve(run.run_id, "cutover", acknowledge_unknown=True).state == WorkflowState.RUNNING


def test_approval_of_a_fresh_run_is_unchanged(store):
    run = store.create(make_run())
    assert Engine(store).approve(run.run_id, "cutover").state == WorkflowState.RUNNING
