"""Cutover ordering and rollback, with the DB, Connect and Worker replaced by fakes."""

import itertools
from contextlib import contextmanager
from pathlib import Path

import pytest

from cdc_agent import jobs
from cdc_agent.config import Settings
from cdc_agent.models import Job, JobRequest

REQUEST = {
    "job_id": "job01",
    "source": {"host": "src", "database": "postgres", "tables": ["public.work_items"], "app_role": "alpha"},
    "target": {"host": "tgt", "database": "app"},
}


class FakeStore:
    def __init__(self, job: Job):
        self.jobs = {job.job_id: job}

    def get(self, job_id):
        return self.jobs.get(job_id)

    def put(self, job):
        self.jobs[job.job_id] = job


class FakeConnect:
    def __init__(self, calls):
        self.calls = calls

    def pause(self, name):
        self.calls.append(("pause", name))

    def resume(self, name):
        self.calls.append(("resume", name))


class FakeWorker:
    def __init__(self, calls, fail_on=None, status=None):
        self.calls = calls
        self.fail_on = fail_on
        self.status_result = status  # dict, or an exception to raise

    def _call(self, name):
        self.calls.append(("worker", name))
        if name == self.fail_on:
            raise RuntimeError(f"{name} failed")

    def hold(self, run_id, max_hold_ms):
        self._call("hold")
        return {"hold_started_at": "t0"}

    def wait_inflight_zero(self, timeout_s):
        self._call("inflight_zero")
        return {"held_count": 0}

    def switch(self, run_id):
        self._call("switch")

    def release(self, run_id):
        self._call("release")
        return {"released_at": "t1", "held_count": 2}

    def status(self):
        self.calls.append(("worker", "status"))
        if isinstance(self.status_result, Exception):
            raise self.status_result
        return self.status_result or {}


@pytest.fixture
def setup(monkeypatch, tmp_path: Path):
    def make(*, mismatch=0, marker_ok=True, worker_fail_on=None, worker_status=None):
        calls: list = []
        job = Job(job_id="job01", request=JobRequest(**REQUEST), state="running", phase="cutover",
                  evidence={"cutover": {"steps": []}})
        settings = Settings("n1", "http://connect", "kafka", "_jobs", 1, tmp_path, tmp_path,
                            "http://worker", None, 2, 10, 3, agent_secrets_dir=tmp_path / "agent")
        runner = jobs.Runner(settings, FakeStore(job))
        runner.connect = FakeConnect(calls)

        @contextmanager
        def lock(ep, job_id):
            yield True

        def wait_marker(target, marker, timeout_s):
            calls.append(("db", "wait_marker"))
            if not marker_ok:
                raise TimeoutError("marker late")
            return 0.1

        def record(name, result=None):
            def fn(*args, **kwargs):
                calls.append(("db", name))
                return result
            return fn

        monkeypatch.setattr(jobs, "Credentials", lambda s, j: type("C", (), {
            "source": None, "source_app": None, "target": None, "target_ddl": None})())
        monkeypatch.setattr(jobs, "WorkerClient", lambda s: FakeWorker(calls, worker_fail_on, worker_status))
        monkeypatch.setattr(jobs.db, "job_lock", lock)

        @contextmanager
        def connect(ep):
            calls.append(("db", "connect"))
            yield None

        monkeypatch.setattr(jobs.db, "connect", connect)
        monkeypatch.setattr(jobs.db, "block_app_writes", record("block", 0))
        monkeypatch.setattr(jobs.db, "unblock_app_writes", record("unblock"))
        monkeypatch.setattr(jobs.db, "insert_marker", record("insert_marker", "m1"))
        monkeypatch.setattr(jobs.db, "wait_marker", wait_marker)
        monkeypatch.setattr(jobs.db, "sequences_of", lambda conn, tables: ["public.t_id_seq"])
        monkeypatch.setattr(jobs.db, "sync_sequences", record("sync_sequences", {"public.t_id_seq": 5}))
        monkeypatch.setattr(jobs.db, "fix_snapshot", record("fix_snapshot"))
        monkeypatch.setattr(jobs.db, "compare_frozen", record("compare", {"mismatch": mismatch, "rows_checked": 10}))
        return runner, calls

    return make


def test_success_runs_steps_in_order(setup):
    runner, calls = setup()
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert calls == [
        ("db", "connect"), ("db", "connect"), ("db", "connect"), ("db", "connect"),
        ("worker", "hold"), ("worker", "inflight_zero"), ("db", "block"), ("db", "insert_marker"),
        ("db", "wait_marker"), ("pause", "job01-sink"), ("db", "sync_sequences"),
        ("db", "fix_snapshot"), ("db", "fix_snapshot"),
        ("worker", "switch"), ("worker", "release"), ("db", "compare"),
    ]
    assert job.metrics["verification"] == "match" and job.metrics["mismatch"] == 0
    assert job.evidence["verification"]["state"] == "match"
    assert job.evidence["validation"]["at"] == "cutover_reference_point"
    assert (job.state, job.phase) == ("running", "stabilizing")
    assert job.metrics["traffic_mode"] == "TARGET"
    assert job.evidence["cutover"]["held_count"] == 2


def test_marker_timeout_returns_writes_to_source(setup):
    runner, calls = setup(marker_ok=False)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("worker", "switch") not in calls
    assert calls[-3:] == [("db", "unblock"), ("resume", "job01-sink"), ("worker", "release")]
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")


def test_mismatch_at_the_switch_needs_a_person_and_never_goes_back(setup):
    runner, calls = setup(mismatch=1)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("db", "unblock") not in calls and ("resume", "job01-sink") not in calls
    assert (job.state, job.phase) == ("failed", "stabilizing")
    assert job.evidence["verification"]["state"] == "mismatch"
    assert job.metrics["mismatch"] == 1


def test_comparison_that_cannot_finish_is_recorded_and_does_not_fail_the_job(setup, monkeypatch):
    runner, calls = setup()

    def broken(*args):
        raise RuntimeError("connection lost, password=hunter2")

    monkeypatch.setattr(jobs.db, "compare_frozen", broken)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert (job.state, job.phase) == ("running", "stabilizing")
    assert job.evidence["verification"]["state"] == "unverifiable"
    assert "hunter2" not in job.evidence["verification"]["reason"]
    assert ("worker", "release") in calls


def test_traffic_is_released_before_the_comparison(setup, monkeypatch):
    runner, calls = setup()
    seen = {}

    def compare(*args):
        stored = runner.store.get("job01")
        seen.update(phase=stored.phase, verification=stored.evidence["verification"]["state"])
        return {"mismatch": 0, "rows_checked": 1}

    monkeypatch.setattr(jobs.db, "compare_frozen", compare)
    runner._cutover("job01")

    assert ("worker", "release") in calls
    assert seen == {"phase": "stabilizing", "verification": "running"}


def test_failed_sequence_sync_returns_writes_to_source(setup, monkeypatch):
    runner, calls = setup()

    def broken(*args):
        raise RuntimeError("must be owner of sequence")

    monkeypatch.setattr(jobs.db, "sync_sequences", broken)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("worker", "switch") not in calls
    assert calls[-3:] == [("db", "unblock"), ("resume", "job01-sink"), ("worker", "release")]
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")


def test_going_past_the_hold_limit_returns_writes_to_source(setup, monkeypatch):
    runner, calls = setup()
    ticks = itertools.count(step=100)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(ticks))
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("worker", "switch") not in calls
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")
    assert "hold limit" in job.message


def test_failure_after_switch_never_goes_back(setup):
    runner, calls = setup(worker_fail_on="release")
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("db", "unblock") not in calls
    assert ("resume", "job01-sink") not in calls
    assert job.state == "failed"


def test_resume_steps_run_even_if_one_fails(setup, monkeypatch):
    runner, calls = setup(marker_ok=False)

    def broken_unblock(app):
        raise RuntimeError("read-only")

    monkeypatch.setattr(jobs.db, "unblock_app_writes", broken_unblock)
    runner._cutover("job01")
    steps = [s["step"] for s in runner.store.get("job01").evidence["cutover"]["steps"]]

    assert "source_writes_unblocked_failed" in steps
    assert calls[-2:] == [("resume", "job01-sink"), ("worker", "release")]


def test_missing_worker_setting_leaves_the_job_waiting_instead_of_stuck(setup, monkeypatch):
    runner, calls = setup()

    def no_worker(settings):
        raise RuntimeError("WORKER_URL is not set; cutover needs the traffic-control API")

    monkeypatch.setattr(jobs, "WorkerClient", no_worker)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")
    assert "WORKER_URL" in job.message
    assert [s["step"] for s in job.evidence["cutover"]["steps"]] == ["failed"]
    assert calls == []


def test_missing_secrets_leave_the_job_waiting_instead_of_stuck(setup, monkeypatch):
    runner, calls = setup()

    def no_secrets(settings, job):
        raise jobs.SecretsNotReady("secrets files missing on this node: job01.properties")

    monkeypatch.setattr(jobs, "Credentials", no_secrets)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")
    assert calls == []


def test_cutover_errors_do_not_carry_the_password(setup, tmp_path, monkeypatch):
    runner, calls = setup()
    runner.settings.agent_secrets_dir.mkdir(exist_ok=True)
    (tmp_path / "job01.properties").write_text("source.password=plain-pw-marker\n")
    (runner.settings.agent_secrets_dir / "job01.properties").write_text("source.admin.password=admin-pw-marker\n")

    def connect(ep):
        raise RuntimeError("connection failed for plain-pw-marker and admin-pw-marker")

    monkeypatch.setattr(jobs.db, "connect", connect)
    runner._cutover("job01")
    job = runner.store.get("job01")
    shown = job.message + str(job.evidence["cutover"]["steps"])

    assert "marker" not in shown and "connection failed" in shown


class StatusWorker:
    def __init__(self, result):
        self.result = result

    def status(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("result, expected", [
    ({"mode": "HOLD"}, "HOLD"),
    ({"mode": "TARGET"}, "TARGET"),
    (RuntimeError("connection refused"), "not usable"),
])
def test_worker_check_refuses_when_cutover_cannot_work(setup, monkeypatch, result, expected):
    runner, _ = setup()
    monkeypatch.setattr(jobs, "WorkerClient", lambda s: StatusWorker(result))

    with pytest.raises(jobs.WorkerNotReady, match=expected):
        runner.check_worker()


def test_worker_check_passes_on_source(setup, monkeypatch):
    runner, _ = setup()
    monkeypatch.setattr(jobs, "WorkerClient", lambda s: StatusWorker({"mode": "SOURCE"}))
    runner.check_worker()


# -- switch 응답이 끊겼을 때: 되돌리기 전에 Worker 의 실제 방향을 확인 ---------------------------------

def _steps(job):
    return [s["step"] for s in job.evidence["cutover"]["steps"]]


def test_lost_switch_reply_with_the_worker_on_target_goes_on(setup):
    runner, calls = setup(worker_fail_on="switch",
                          worker_status={"mode": "HOLD", "next_origin": "TARGET", "run_id": "job01"})
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("db", "unblock") not in calls and ("resume", "job01-sink") not in calls  # 원본 쓰기를 다시 열지 않음
    assert calls[-4:] == [("worker", "switch"), ("worker", "status"), ("worker", "release"), ("db", "compare")]
    assert "switch_confirmed_by_status" in _steps(job)
    assert (job.state, job.phase) == ("running", "stabilizing")


def test_lost_switch_reply_after_the_hold_already_released_to_target_goes_on(setup):
    runner, calls = setup(worker_fail_on="switch",
                          worker_status={"mode": "TARGET", "next_origin": "TARGET", "run_id": "job01"})
    runner._cutover("job01")
    assert runner.store.get("job01").phase == "stabilizing"
    assert ("db", "unblock") not in calls


def test_failed_switch_with_the_worker_still_on_source_returns_writes_to_source(setup):
    runner, calls = setup(worker_fail_on="switch",
                          worker_status={"mode": "HOLD", "next_origin": "SOURCE", "run_id": "job01"})
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert calls[-3:] == [("db", "unblock"), ("resume", "job01-sink"), ("worker", "release")]
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")


@pytest.mark.parametrize("status", [
    RuntimeError("status timed out"),  # 방향을 알 수 없음
    {"mode": "TARGET", "next_origin": "TARGET", "run_id": "other"},  # 다른 작업이 잡고 있음
])
def test_unknown_worker_direction_keeps_source_writes_blocked_for_a_person(setup, status):
    runner, calls = setup(worker_fail_on="switch", worker_status=status)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("db", "unblock") not in calls and ("resume", "job01-sink") not in calls
    assert ("worker", "release") not in calls
    assert job.state == "failed" and "수동 확인" in job.message
