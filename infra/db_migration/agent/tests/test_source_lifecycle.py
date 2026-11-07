"""Source preparation, abort and cleanup, with the DB and Connect replaced by fakes."""

from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg
import pytest

from cdc_agent import jobs
from cdc_agent.config import Settings
from cdc_agent.models import Job, JobRequest

REQUEST = {
    "job_id": "job01",
    "source": {"host": "src", "database": "postgres", "tables": ["public.work_items"], "app_role": "alpha"},
    "target": {"host": "tgt", "database": "app", "app_role": "app_user"},
}
CONNECT_PROPS = "\n".join(f"{a}.{f}={a}-{f}" for a in ("source", "target") for f in ("user", "password"))
AGENT_PROPS = "\n".join(f"{a}.{f}={a}-{f}"
                        for a in ("source.app", "source.admin", "target.ddl", "target.admin")
                        for f in ("user", "password"))


class FakeStore:
    def __init__(self, job: Job):
        self.jobs = {job.job_id: job}

    def get(self, job_id):
        return self.jobs[job_id].model_copy(deep=True)

    def all(self):
        return self.jobs

    def put(self, job):
        self.jobs[job.job_id] = job


class FakeConnect:
    def __init__(self, calls):
        self.calls = calls

    def delete(self, name):
        self.calls.append(("connect_delete", name))


class FakeSecrets:
    def __init__(self, calls):
        self.calls = calls

    def sync(self):
        self.calls.append(("secrets_sync",))

    def delete(self, job_id):
        self.calls.append(("secrets_delete", job_id))


@pytest.fixture
def setup(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    def make(*, phase="streaming", state="running", secrets_file=True, locked=True):
        calls: list = []
        job = Job(job_id="job01", request=JobRequest(**REQUEST), state=state, phase=phase)
        settings = Settings("n1", "http://connect", "kafka", "_jobs", 1, tmp_path, tmp_path,
                            "http://worker", None, 2, 10, 3, agent_secrets_dir=tmp_path / "agent")
        settings.agent_secrets_dir.mkdir(exist_ok=True)
        if secrets_file:
            (tmp_path / "job01.properties").write_text(CONNECT_PROPS)
            (settings.agent_secrets_dir / "job01.properties").write_text(AGENT_PROPS)
        runner = jobs.Runner(settings, FakeStore(job), FakeSecrets(calls))
        runner.connect = FakeConnect(calls)

        @contextmanager
        def lock(ep, job_id):
            calls.append(("lock", ep.user))
            yield locked

        monkeypatch.setattr(jobs.db, "job_lock", lock)
        monkeypatch.setattr(jobs.db, "retire_target_accounts", lambda ep, users: calls.append(
            ("retire", ep.user, users)) or users)
        monkeypatch.setattr(jobs.db, "drop_slot", lambda ep, slot: calls.append(("drop_slot", ep.user)) or True)
        monkeypatch.setattr(jobs.db, "cleanup_source", lambda ep, job, repl: calls.append(
            ("cleanup_source", ep.user, repl)) or {"role_dropped": True})
        return runner, calls

    return make


def test_new_job_starts_with_source_prepare():
    assert Job(job_id="job01", request=JobRequest(**REQUEST)).phase == "source_prepare"


def test_source_prepare_runs_as_admin_because_replication_account_is_not_there_yet(setup, monkeypatch):
    runner, calls = setup(phase="source_prepare", state="pending")
    monkeypatch.setattr(jobs.db, "prepare_source", lambda ep, job, user, pw: calls.append(
        ("prepare", ep.user, user, pw)))
    runner.advance("job01")
    job = runner.store.get("job01")

    assert ("lock", "source.admin-user") in calls
    assert ("prepare", "source.admin-user", "source-user", "source-password") in calls
    assert (job.state, job.phase, job.progress) == ("running", "target_prepare", 2)


def test_target_prepare_makes_the_two_accounts_as_the_target_admin(setup, monkeypatch):
    runner, calls = setup(phase="target_prepare")
    monkeypatch.setattr(jobs.db, "prepare_target", lambda admin, ddl, sink: calls.append(
        ("prepare_target", admin.user, ddl.user, ddl.password, sink.user, sink.password)))
    runner.advance("job01")
    job = runner.store.get("job01")

    assert ("prepare_target", "target.admin-user", "target.ddl-user", "target.ddl-password",
            "target-user", "target-password") in calls
    assert (job.state, job.phase) == ("running", "precheck")


def test_failed_target_prepare_can_be_retried_and_names_the_step(setup, monkeypatch):
    runner, _ = setup(phase="target_prepare")

    def refuse(admin, ddl, sink):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(jobs.db, "prepare_target", refuse)
    runner.advance("job01")
    job = runner.store.get("job01")

    assert (job.state, job.phase) == ("failed", "target_prepare")
    assert job.message == "target_prepare: permission denied"
    assert job.phase in jobs.SETUP_PHASES


def test_secrets_not_on_this_node_yet_does_not_fail_the_job(setup):
    runner, calls = setup(phase="source_prepare", state="pending", secrets_file=False)
    runner.advance("job01")

    assert runner.store.get("job01").state == "pending"
    assert calls == []


def test_step_does_not_run_on_a_job_aborted_while_waiting_for_the_lock(setup, monkeypatch):
    runner, calls = setup(phase="streaming", state="aborted")
    monkeypatch.setattr(runner, "_step", lambda job, creds: calls.append(("step",)))
    monkeypatch.setattr(jobs, "TERMINAL_STATES", set())  # the first read still saw it running
    runner.advance("job01")

    assert ("step",) not in calls


def test_failure_after_an_abort_keeps_aborted(setup, monkeypatch):
    runner, calls = setup(phase="streaming", state="running")

    @contextmanager
    def lock(ep, job_id):
        job = runner.store.get("job01")
        job.report(state="aborted")
        runner.store.put(job)
        raise RuntimeError("role does not exist")
        yield

    monkeypatch.setattr(jobs.db, "job_lock", lock)
    runner.advance("job01")

    assert runner.store.get("job01").state == "aborted"


def test_abort_cleans_everything_in_order(setup):
    runner, calls = setup()
    job = runner.abort("job01")

    assert [c[0] for c in calls] == [
        "secrets_sync", "lock", "connect_delete", "connect_delete", "drop_slot", "cleanup_source", "retire",
        "secrets_delete",
    ]
    assert ("cleanup_source", "source.admin-user", "source-user") in calls
    assert job.state == "aborted"
    assert job.evidence["cleanup"]["slot_dropped"] is True
    assert job.evidence["cleanup"]["role_dropped"] is True


def test_abort_failure_keeps_the_job_and_the_secrets_for_another_try(setup, monkeypatch):
    runner, calls = setup()

    def broken(ep, job, repl):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(jobs.db, "cleanup_source", broken)
    with pytest.raises(RuntimeError, match="unreachable"):
        runner.abort("job01")

    assert runner.store.get("job01").state == "running"
    assert ("secrets_delete", "job01") not in calls


def test_abort_when_another_agent_holds_the_job(setup, monkeypatch):
    runner, calls = setup(locked=False)
    monkeypatch.setattr(jobs, "ABORT_LOCK_WAIT_S", -1)
    with pytest.raises(jobs.JobBusy):
        runner.abort("job01")

    assert not any(c[0] == "connect_delete" for c in calls)


def test_abort_fails_while_the_slot_stays_active(setup, monkeypatch):
    runner, calls = setup()
    monkeypatch.setattr(jobs, "SLOT_DROP_WAIT_S", -1)
    monkeypatch.setattr(jobs.db, "drop_slot", lambda ep, slot: False)
    monkeypatch.setattr(jobs.db, "slot_exists", lambda ep, slot: True)
    with pytest.raises(RuntimeError, match="still active"):
        runner.abort("job01")

    assert not any(c[0] == "cleanup_source" for c in calls)


def test_abort_with_a_slot_already_gone(setup, monkeypatch):
    runner, calls = setup()
    monkeypatch.setattr(jobs.db, "drop_slot", lambda ep, slot: False)
    monkeypatch.setattr(jobs.db, "slot_exists", lambda ep, slot: False)
    job = runner.abort("job01")

    assert job.state == "aborted"
    assert job.evidence["cleanup"]["slot_dropped"] is False


def test_abort_before_secrets_arrived_skips_the_databases(setup):
    runner, calls = setup(phase="source_prepare", state="pending", secrets_file=False)
    job = runner.abort("job01")

    assert job.state == "aborted"
    assert not any(c[0] in ("lock", "cleanup_source") for c in calls)


RUNNING = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}
TASK_FAILED = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "FAILED", "trace": "boom\nat line"}]}


def compared(source_rows, target_rows, same=False):
    row = {"source": {"rows": source_rows, "checksum": None}, "target": {"rows": target_rows, "checksum": None},
           "match": same}
    return {"tables": {"public.work_items": row}, "mismatch": 0 if same else 1, "rows_checked": source_rows}


@pytest.fixture
def watched(setup, monkeypatch):
    def make(check, *, phase="snapshot", status=RUNNING):
        runner, calls = setup(phase=phase)
        runner.connect.status = lambda name: status
        monkeypatch.setattr(jobs.db, "compare", lambda *a: check() if callable(check) else check)
        monkeypatch.setattr(jobs.db, "slot_lag_bytes", lambda *a: 0)
        return runner, calls

    return make


@pytest.mark.parametrize("error", [psycopg.OperationalError("connection lost"), httpx.ConnectError("no route")])
def test_a_short_outage_while_syncing_does_not_end_the_job(watched, error):
    def down():
        raise error

    runner, _ = watched(down, phase="streaming")
    runner.advance("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase) == ("running", "streaming")
    assert str(error) in job.metrics["measure_error"]

    runner, _ = watched(compared(10, 10, same=True), phase="streaming")
    runner.store.jobs["job01"] = job
    runner.advance("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")
    assert "measure_error" not in job.metrics


def test_a_failed_connector_task_still_fails_the_job(watched):
    runner, _ = watched(compared(10, 10, same=True), phase="streaming", status=TASK_FAILED)
    runner.advance("job01")
    job = runner.store.get("job01")
    assert job.state == "failed"
    assert job.message == f"streaming: {job.source_connector}: boom"


@pytest.fixture
def waiting(setup, monkeypatch):
    """A job waiting for approval, with a controllable clock. Records each row count."""
    runner, _ = setup(phase="cutover_ready")
    job = runner.store.get("job01")
    job.state = "waiting_approval"
    job.metrics = {"mismatch": 0, "evidence_of": "reconcile"}
    clock = {"now": 1000.0, "calls": [], "fail": None}

    def compare(*args, checksum=True):
        clock["calls"].append(checksum)
        if clock["fail"]:
            raise clock["fail"]
        return compared(10 + len(clock["calls"]), 10)

    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(jobs.db, "compare", compare)
    monkeypatch.setattr(jobs.db, "slot_lag_bytes", lambda *a: 0)
    return runner, job, clock


def test_polling_does_not_measure_again_within_the_interval(waiting):
    runner, job, clock = waiting

    first = runner.refresh_metrics(job.model_copy(deep=True))
    clock["now"] += runner.settings.metrics_interval_s - 1
    second = runner.refresh_metrics(job.model_copy(deep=True))

    assert clock["calls"] == [False]  # one count, no checksum
    assert second.metrics["rows_checked"] == first.metrics["rows_checked"] == 11

    clock["now"] += 1
    third = runner.refresh_metrics(job.model_copy(deep=True))
    assert clock["calls"] == [False, False]
    assert third.metrics["rows_checked"] == 12


def test_a_failed_measurement_keeps_the_last_numbers_until_the_next_try(waiting):
    runner, job, clock = waiting
    runner.refresh_metrics(job.model_copy(deep=True))

    clock["fail"] = psycopg.OperationalError("connection lost")
    clock["now"] += runner.settings.metrics_interval_s
    failed = runner.refresh_metrics(job.model_copy(deep=True))
    assert "connection lost" in failed.metrics["measure_error"]
    assert failed.metrics["rows_checked"] == 11
    assert len(clock["calls"]) == 2

    runner.refresh_metrics(job.model_copy(deep=True))
    assert len(clock["calls"]) == 2

    clock["fail"] = None
    clock["now"] += runner.settings.metrics_interval_s
    assert "measure_error" not in runner.refresh_metrics(job.model_copy(deep=True)).metrics


@pytest.mark.parametrize("phase", ["snapshot", "streaming", "cutover", "stabilizing"])
def test_only_waiting_for_approval_measures_on_a_read(waiting, phase):
    runner, job, clock = waiting
    job.phase = phase

    assert runner.refresh_metrics(job).metrics == {"mismatch": 0, "evidence_of": "reconcile"}
    assert clock["calls"] == []


def test_snapshot_lasts_until_the_rows_of_the_start_have_arrived(watched):
    checks = iter([compared(1000, 0), compared(1200, 999), compared(1300, 1000)])
    runner, _ = watched(lambda: next(checks))

    runner.advance("job01")
    assert runner.store.get("job01").phase == "snapshot"
    assert runner.store.get("job01").outputs["copy_baseline"] == {"public.work_items": 1000}
    runner.advance("job01")
    assert runner.store.get("job01").phase == "snapshot"
    # behind only because of live writes
    runner.advance("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase, job.progress) == ("running", "streaming", 50)


def test_snapshot_waits_for_running_connectors(watched):
    runner, _ = watched(compared(10, 10), status={"connector": {"state": "RUNNING"}, "tasks": []})
    runner.advance("job01")
    assert runner.store.get("job01").phase == "snapshot"


def test_rows_deleted_during_the_copy_do_not_hold_it_back():
    check = compared(800, 800)
    assert jobs.db.copy_done(check, {"public.work_items": 1000})
    assert not jobs.db.copy_done(compared(800, 799), {"public.work_items": 1000})
    assert not jobs.db.copy_done(compared(5, None), {"public.work_items": 5})


def test_connector_failure_while_waiting_for_approval_fails_the_job(watched):
    runner, _ = watched(None, phase="cutover_ready", status=TASK_FAILED)
    runner.store.jobs["job01"].state = "waiting_approval"
    runner.watch_ready("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase) == ("failed", "streaming")  # a retry starts over from the checks
    assert "boom" in job.message


def test_watching_the_ready_job_takes_no_lock_and_writes_nothing_when_healthy(watched):
    runner, calls = watched(None, phase="cutover_ready")
    runner.store.jobs["job01"].state = "waiting_approval"
    before = runner.store.jobs["job01"]
    runner.watch_ready("job01")
    assert runner.store.jobs["job01"] is before
    assert not [c for c in calls if c[0] == "lock"]


def test_connect_outage_while_waiting_for_approval_changes_nothing(watched):
    runner, _ = watched(None, phase="cutover_ready")
    runner.store.jobs["job01"].state = "waiting_approval"

    def down(name):
        raise httpx.ConnectError("no route")

    runner.connect.status = down
    runner.watch_ready("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase) == ("waiting_approval", "cutover_ready")


def test_a_failure_seen_after_the_approval_does_not_touch_the_cutover(watched):
    runner, _ = watched(None, phase="cutover_ready", status=TASK_FAILED)
    runner.store.jobs["job01"].state = "waiting_approval"
    original = runner.connect.status

    def approved_meanwhile(name):
        job = runner.store.jobs["job01"]
        job.state, job.phase = "running", "cutover"
        return original(name)

    runner.connect.status = approved_meanwhile
    runner.watch_ready("job01")
    job = runner.store.get("job01")
    assert (job.state, job.phase) == ("running", "cutover")


def test_retry_restarts_failed_connectors_only_once_they_exist(setup):
    restarted = []
    runner, _ = setup(phase="streaming", state="failed")
    runner.connect.restart_failed = restarted.append
    job = runner.store.get("job01")
    runner.restart_failed_connectors(job)
    assert restarted == [job.source_connector, job.sink_connector]

    restarted.clear()
    job.phase = "target_prepare"
    runner.restart_failed_connectors(job)
    assert restarted == []


def test_close_takes_login_from_the_target_accounts_before_the_secrets_go(setup):
    runner, calls = setup(phase="stabilizing")
    job = runner.store.get("job01")
    runner.close(job)
    names = [c[0] for c in calls]

    assert ("retire", "target.admin-user", ["target-user", "target.ddl-user"]) in calls
    assert names.index("retire") < names.index("secrets_delete")
    assert job.evidence["cleanup"]["target_accounts"] == {"retired": ["target-user", "target.ddl-user"]}


def test_abort_takes_login_from_the_target_accounts_too(setup):
    runner, calls = setup()
    runner.abort("job01")
    assert any(c[0] == "retire" for c in calls)


def test_app_account_keeps_its_login(setup):
    runner, calls = setup(phase="stabilizing")
    job = runner.store.get("job01")
    job.request.target.app_role = "target-user"
    runner.close(job)

    assert ("retire", "target.admin-user", ["target.ddl-user"]) in calls


def test_accounts_the_caller_gave_keep_their_login(setup):
    runner, calls = setup(phase="stabilizing")
    job = runner.store.get("job01")
    job.request.target.keep_sink_user = True
    runner.close(job)

    assert ("retire", "target.admin-user", ["target.ddl-user"]) in calls


def test_accounts_stay_while_another_job_uses_the_same_target(setup):
    runner, calls = setup(phase="stabilizing")
    job = runner.store.get("job01")
    other = job.model_copy(deep=True, update={"job_id": "job02", "state": "running"})
    runner.store.put(other)
    runner.close(job)

    assert not any(c[0] == "retire" for c in calls)
    assert job.evidence["cleanup"]["target_accounts"] == {
        "kept": ["target-user", "target.ddl-user"], "used_by": ["job02"]}

    runner.store.put(other.model_copy(update={"state": "succeeded"}))
    second = runner.store.get("job01")
    runner.close(second)
    assert any(c[0] == "retire" for c in calls)


def test_close_waits_for_the_comparison_while_its_agent_holds_the_lock(setup):
    runner, calls = setup(phase="stabilizing", locked=False)
    job = runner.store.get("job01")
    job.evidence["verification"] = {"state": "running"}

    with pytest.raises(jobs.CloseRefused, match="still running"):
        runner.close(job)
    assert not any(c[0] == "drop_slot" for c in calls)


def test_close_goes_on_when_the_agent_comparing_has_stopped(setup):
    runner, calls = setup(phase="stabilizing")
    job = runner.store.get("job01")
    job.evidence["verification"] = {"state": "running"}
    runner.store.put(job.model_copy(deep=True))

    runner.close(job)

    assert job.state == "succeeded" and job.evidence["verification"]["state"] == "unverifiable"
    assert any(c[0] == "drop_slot" for c in calls)


def test_close_is_refused_after_a_mismatch(setup):
    runner, calls = setup(phase="stabilizing", state="failed")
    job = runner.store.get("job01")
    job.message = "mismatch"

    with pytest.raises(jobs.CloseRefused, match="mismatch"):
        runner.close(job)
    assert not any(c[0] == "drop_slot" for c in calls)


def test_failure_to_retire_is_reported_and_does_not_undo_the_cleanup(setup, monkeypatch):
    runner, calls = setup(phase="stabilizing")

    def broken(ep, users):
        raise RuntimeError("no route, target.admin-password")

    monkeypatch.setattr(jobs.db, "retire_target_accounts", broken)
    job = runner.store.get("job01")
    runner.close(job)
    accounts = job.evidence["cleanup"]["target_accounts"]

    assert job.state == "succeeded" and accounts["accounts"] == ["target-user", "target.ddl-user"]
    assert "no route" in accounts["error"] and "target.admin-password" not in accounts["error"]
    assert ("secrets_delete", "job01") in calls
