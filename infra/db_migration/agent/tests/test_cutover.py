"""Cutover ordering and rollback, with the DB, Connect and Worker replaced by fakes."""

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
    def __init__(self, calls, fail_on=None):
        self.calls = calls
        self.fail_on = fail_on

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


@pytest.fixture
def setup(monkeypatch, tmp_path: Path):
    def make(*, mismatch=0, marker_ok=True, worker_fail_on=None):
        calls: list = []
        job = Job(job_id="job01", request=JobRequest(**REQUEST), state="running", phase="cutover",
                  evidence={"cutover": {"steps": []}})
        settings = Settings("n1", "http://connect", "kafka", "_jobs", 1, tmp_path, tmp_path,
                            "http://worker", None, 2, 10, 3)
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
            "source": None, "source_app": None, "target": None})())
        monkeypatch.setattr(jobs, "WorkerClient", lambda s: FakeWorker(calls, worker_fail_on))
        monkeypatch.setattr(jobs.db, "job_lock", lock)
        monkeypatch.setattr(jobs.db, "block_app_writes", record("block", 0))
        monkeypatch.setattr(jobs.db, "unblock_app_writes", record("unblock"))
        monkeypatch.setattr(jobs.db, "insert_marker", record("insert_marker", "m1"))
        monkeypatch.setattr(jobs.db, "wait_marker", wait_marker)
        monkeypatch.setattr(jobs.db, "compare", record("compare", {"mismatch": mismatch, "rows_checked": 10}))
        return runner, calls

    return make


def test_success_runs_steps_in_order(setup):
    runner, calls = setup()
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert calls == [
        ("worker", "hold"), ("worker", "inflight_zero"), ("db", "block"), ("db", "insert_marker"),
        ("db", "wait_marker"), ("pause", "job01-sink"), ("db", "compare"),
        ("worker", "switch"), ("worker", "release"),
    ]
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


def test_mismatch_at_reference_point_does_not_switch(setup):
    runner, calls = setup(mismatch=1)
    runner._cutover("job01")
    job = runner.store.get("job01")

    assert ("worker", "switch") not in calls
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")
    assert "mismatch" in job.message


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
