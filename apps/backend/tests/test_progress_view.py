"""The values the module gives out: same form on every route, nothing guessed."""

import pytest

from app.features.db_migration.progress_view import (
    clamp_progress,
    classify_cutover,
    network_exposure,
    to_metrics,
)


def steps(*names, **first):
    items = [{"step": name, "at": "2026-11-07T00:00:00+00:00"} for name in names]
    items[0].update(first)
    return {"cutover": {"steps": items}}


FULL_SEQUENCE = ("hold", "marker_on_target", "snapshot_fixed", "switched_to_target", "released")


def test_only_the_agreed_keys_leave_the_module():
    metrics = to_metrics({
        "state": "running",
        "phase": "streaming",
        "metrics": {
            "phase": "snapshot",
            "copy_percent": 99.5,
            "replication_lag_bytes": 12,
            "mismatch": 1,
            "rows_checked": 1000,
            "measured_at": "2026-11-07T00:00:00+00:00",
            "tables": {"public.a": {"source_rows": 2, "target_rows": 1, "percent": 50.0, "match": False, "x": 1}},
            "measure_error": "password=secret",
            "something_new": 1,
        },
    }, lambda text: "[cleaned]")

    assert metrics == {
        "phase": "streaming",
        "copy_percent": 99.5,
        "replication_lag_bytes": 12,
        "mismatch": 1,
        "rows_checked": 1000,
        "measured_at": "2026-11-07T00:00:00+00:00",
        "measure_error": "[cleaned]",
        "tables": {"public.a": {"source_rows": 2, "target_rows": 1, "percent": 50.0, "match": False}},
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"copy_percent": 101},
        {"copy_percent": True},
        {"copy_percent": float("nan")},
        {"replication_lag_bytes": -1},
        {"replication_lag_bytes": "12"},
        {"mismatch": None},
        {"measured_at": "2026-11-07T00:00:00"},
        {"measured_at": "yesterday"},
        {"tables": {"public.a": {"source_rows": "2", "target_rows": 1}}},
        {"tables": []},
        {"verification": "fine"},
        {"simulated": "yes"},
        {"traffic_mode": "SOURCE"},
        {"cutover_ms": -1},
    ],
)
def test_a_wrong_value_is_left_out(raw):
    assert to_metrics({"phase": "streaming", "metrics": raw}) == {"phase": "streaming"}


@pytest.mark.parametrize("job", [{}, {"metrics": None, "evidence": None}, {"metrics": [], "evidence": {"cutover": 1}}])
def test_a_broken_job_raises_nothing(job):
    assert to_metrics(job) == {}


def test_traffic_is_only_reported_after_the_switch_and_by_role():
    before = to_metrics({"phase": "cutover_ready", "metrics": {}})
    after = to_metrics({"phase": "cutover", "metrics": {"traffic_mode": "TARGET", "cutover_ms": 1234}})

    assert "serving" not in before
    assert after["serving"] == "target"
    assert after["cutover_seconds"] == 1.234 and after["cutover_seconds_source"] == "agent"


def test_cutover_steps_keep_allowed_keys_and_clean_the_error():
    job = {
        "state": "running",
        "phase": "cutover",
        "evidence": steps("hold", "failed", waited_ms=5, held_count=2, error="password=secret"),
    }

    metrics = to_metrics(job, lambda text: text.replace("secret", "***"))

    assert metrics["cutover_steps"] == [{"step": "hold", "waited_ms": 5, "error": "password=***"}, {"step": "failed"}]


def test_sequence_is_given_only_after_the_switch():
    running = {"state": "running", "phase": "cutover", "evidence": steps("hold")}
    complete = {"state": "running", "phase": "stabilizing", "evidence": steps(*FULL_SEQUENCE)}
    partial = {"state": "running", "phase": "stabilizing", "evidence": steps("hold", "released")}

    assert "cutover_sequence" not in to_metrics(running)
    assert to_metrics(complete)["cutover_sequence"] == "VERIFIED"
    assert to_metrics(partial)["cutover_sequence"] == "INCOMPLETE"
    assert to_metrics(partial)["cutover_sequence_missing"] == ["marker_on_target", "snapshot_fixed", "switched_to_target"]


def test_failure_before_the_cutover_is_an_error_not_a_cutover_outcome():
    metrics = to_metrics({"state": "failed", "phase": "snapshot", "message": "password=secret"},
                         lambda text: text.replace("secret", "***"))

    assert metrics == {"phase": "snapshot", "cdc_error": "password=***"}


@pytest.mark.parametrize(
    ("job", "confirm", "expected"),
    [
        ({"state": "failed", "phase": "cutover"}, None, "post_switch_failed"),
        ({"state": "failed", "phase": "stabilizing"}, None, "unknown"),
        ({"state": "aborted", "phase": "cutover"}, None, "unknown"),
        ({"state": "running", "phase": "stabilizing"}, None, "switched"),
        ({"state": "succeeded", "phase": "closed"}, None, "switched"),
        ({"state": "waiting_approval", "phase": "cutover_ready", "evidence": steps("failed", "x_failed")}, True, "unknown"),
        ({"state": "waiting_approval", "phase": "cutover_ready", "evidence": steps("switched_to_target", "failed")}, True, "unknown"),
        ({"state": "waiting_approval", "phase": "cutover_ready", "evidence": steps("hold", "failed")}, True, "rolled_back"),
        ({"state": "waiting_approval", "phase": "cutover_ready", "evidence": steps("hold", "failed")}, False, "unknown"),
        ({"state": "waiting_approval", "phase": "cutover_ready"}, True, None),
        ({"state": "running", "phase": "cutover", "evidence": steps("hold")}, True, None),
    ],
)
def test_classify_cutover(job, confirm, expected):
    assert classify_cutover(job, lambda: confirm) == expected


def test_rollback_needs_a_confirmation():
    job = {"state": "waiting_approval", "phase": "cutover_ready", "evidence": steps("hold", "failed")}

    def broken():
        raise OSError("worker down")

    assert classify_cutover(job) == "unknown"
    assert classify_cutover(job, broken) == "unknown"


@pytest.mark.parametrize(
    ("address", "expected"),
    [("10.0.0.5", "PRIVATE"), ("8.8.8.8", "PUBLIC"), ("db.example.internal", None), ("", None), (None, None)],
)
def test_network_exposure(address, expected):
    assert network_exposure(address) == expected


@pytest.mark.parametrize(("value", "expected"), [(-5, 0), (42, 42), (150, 100), (None, 0), (True, 0)])
def test_clamp_progress(value, expected):
    assert clamp_progress(value) == expected
