import logging
from dataclasses import replace

import httpx
import pytest

from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient
from app.features.db_migration.guide_workflow import Steps, run_migration, stop_migration, to_job_id

RUN_ID = "mig-20261105-001"
JOB_ID = "mig_20261105_001"
SOURCE = {
    "db": {"host": "source.example", "port": 5432, "database": "postgres"},
    "app": {"db_user": "alpha"},
    "tables": ["public.work_items"],
}
TARGET = {"db": {"private_ip": "10.0.0.5", "port": 5432, "database": "app", "ssl_mode": "ENCRYPTED_ONLY"}}


class FakeAgent:
    """Walks a job through the given phases, one per GET."""

    def __init__(self, phases=(("snapshot", 20), ("streaming", 50), ("cutover_ready", 70))):
        self.phases = list(phases)
        self.calls = []
        self.requests = []
        self.state = "running"
        self.message = ""
        self.errors = []
        self.created = False

    def client(self) -> CdcAgentClient:
        settings = AgentSettings(url="https://cdc.example")
        return CdcAgentClient(settings, transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if request.method == "POST" and path == "/jobs":
            self.created = True
            self.requests.append(request.content)
            return httpx.Response(202, json={"job_id": JOB_ID})
        if not self.created:
            return httpx.Response(404, json={"detail": "not found"})
        if path.endswith("/abort"):
            if self.state in ("aborted", "failed"):
                return httpx.Response(409, json={"detail": "abort is allowed only before cutover"})
            self.state = "aborted"
            return httpx.Response(200, json=self.job())
        if self.errors:
            return httpx.Response(self.errors.pop(0), json={"detail": "bad gateway"})
        return httpx.Response(200, json=self.job(advance=True))

    def job(self, advance: bool = False) -> dict:
        phase, progress = self.phases[0]
        body = {"job_id": JOB_ID, "state": self.state, "phase": phase, "progress": progress,
                "message": self.message, "metrics": {"mismatch": 0}, "outputs": {"tables": 1}, "evidence": {}}
        if advance and len(self.phases) > 1:
            self.phases.pop(0)
        return body


class Recorder:
    def __init__(self, on_report=None):
        self.reports = []
        self.on_report = on_report

    def report(self, state, progress, metrics=None, message=None):
        self.reports.append((state, progress))
        if self.on_report:
            self.on_report(progress)


class Cancelled(BaseException):
    pass


def make_steps(order: list[str]) -> Steps:
    return Steps(
        ensure_vpn=lambda job_id, source, target: order.append("ensure_vpn"),
        probe=lambda source, target: order.append("probe"),
        deliver_secrets=lambda job_id, source, target: order.append("deliver_secrets"),
        destroy_vpn=lambda job_id: order.append("destroy_vpn"),
    )


def run(agent: FakeAgent, steps: Steps, ctx: Recorder):
    return run_migration(RUN_ID, SOURCE, TARGET, agent=agent.client(), steps=steps, ctx=ctx,
                         poll_s=0, log_interval_s=0)


def test_to_job_id_matches_agent_rule():
    assert to_job_id(RUN_ID) == JOB_ID


def test_runs_in_order_keeps_streaming_until_stop_then_removes_vpn():
    fake, order = FakeAgent(), []
    steps = make_steps(order)
    streamed = []

    def stop_after_three_rounds(progress):
        if progress == 70:
            streamed.append(progress)
            if len(streamed) == 3:
                stop_migration(RUN_ID, agent=fake.client(), steps=steps, run_alive=True)

    result = run(fake, steps, Recorder(stop_after_three_rounds))

    assert order == ["ensure_vpn", "probe", "deliver_secrets", "destroy_vpn"]
    assert result["state"] == "stopped"
    assert result["vpn_destroyed"] is True
    assert result["outputs"] == {"tables": 1}
    assert len(streamed) == 3  # ready에서 끝나지 않고 stop까지 돈다


def test_job_request_uses_only_agent_fields():
    fake, order = FakeAgent(phases=(("cutover_ready", 70),)), []
    steps = make_steps(order)
    ctx = Recorder(lambda p: stop_migration(RUN_ID, agent=fake.client(), steps=steps, run_alive=True))
    run(fake, steps, ctx)

    body = fake.requests[0].decode()
    assert '"host":"source.example"' in body.replace(" ", "")
    assert '"host":"10.0.0.5"' in body.replace(" ", "")
    assert "credentials_ref" not in body
    assert "ENCRYPTED_ONLY" not in body  # Cloud SQL 설정값은 libpq sslmode가 아니다


def test_cancel_from_report_cleans_up_and_reraises():
    fake, order = FakeAgent(), []

    def cancel(progress):
        if progress == 50:
            raise Cancelled()

    with pytest.raises(Cancelled):
        run(fake, make_steps(order), Recorder(cancel))

    assert f"POST /jobs/{JOB_ID}/abort" in fake.calls
    assert order[-1] == "destroy_vpn"


def test_cancel_cleanup_does_not_report_again():
    fake, order = FakeAgent(), []
    ctx = Recorder()

    def cancel(progress):
        if progress == 50:
            raise Cancelled()

    ctx.on_report = cancel
    with pytest.raises(Cancelled):
        run(fake, make_steps(order), ctx)
    assert ctx.reports[-1] == ("running", 50)


def test_failed_job_raises_and_keeps_vpn_for_retry():
    fake, order = FakeAgent(), []
    fake.state = "failed"
    fake.message = "precheck: wal_level"

    with pytest.raises(AgentError, match="precheck: wal_level"):
        run(fake, make_steps(order), Recorder())

    assert "destroy_vpn" not in order
    assert not any(call.endswith("/abort") for call in fake.calls)


def test_temporary_agent_errors_do_not_end_the_run():
    fake, order = FakeAgent(phases=(("cutover_ready", 70),)), []
    fake.errors = [502, 503]
    steps = make_steps(order)
    ctx = Recorder(lambda p: stop_migration(RUN_ID, agent=fake.client(), steps=steps, run_alive=True))

    assert run(fake, steps, ctx)["state"] == "stopped"


def test_unbuilt_steps_fail_instead_of_pretending():
    fake = FakeAgent()
    with pytest.raises(NotImplementedError, match="ensure_vpn"):
        run(fake, Steps(), Recorder())
    assert "POST /jobs" not in fake.calls


def test_vpn_delete_failure_is_reported_not_hidden():
    fake, order = FakeAgent(phases=(("cutover_ready", 70),)), []

    def broken(job_id):
        raise RuntimeError("terraform destroy failed")

    steps = Steps(
        ensure_vpn=lambda *a: None, probe=lambda *a: None, deliver_secrets=lambda *a: None, destroy_vpn=broken
    )
    ctx = Recorder(lambda p: stop_migration(RUN_ID, agent=fake.client(), steps=steps, run_alive=True))
    result = run(fake, steps, ctx)

    assert result["vpn_destroyed"] is False
    assert "terraform destroy failed" in result["vpn_error"]


def test_stop_without_running_run_removes_vpn_itself():
    fake, order = FakeAgent(), []
    fake.created = True
    stop_migration(RUN_ID, agent=fake.client(), steps=make_steps(order), run_alive=False)
    assert order == ["destroy_vpn"]
    assert fake.state == "aborted"


def test_stop_before_job_exists_still_removes_vpn():
    fake, order = FakeAgent(), []
    stop_migration(RUN_ID, agent=fake.client(), steps=make_steps(order), run_alive=False)
    assert order == ["destroy_vpn"]


def test_stop_refuses_job_past_cutover():
    fake, order = FakeAgent(phases=(("stabilizing", 90),)), []
    fake.created = True
    original = fake.handle

    def refuse_abort(request):
        if request.url.path.endswith("/abort"):
            return httpx.Response(409, json={"detail": "abort is allowed only before cutover"})
        return original(request)

    fake.handle = refuse_abort
    with pytest.raises(AgentError):
        stop_migration(RUN_ID, agent=fake.client(), steps=make_steps(order), run_alive=False)
    assert order == []


def test_cancel_keeps_the_vpn_when_cleanup_fails():
    order: list[str] = []
    fake = FakeAgent()

    def broken_abort(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/abort"):
            return httpx.Response(502, json={"detail": "cleanup failed"})
        return FakeAgent.handle(fake, request)

    fake.handle = broken_abort

    def on_report(progress):
        if progress == 50:
            raise Cancelled()

    with pytest.raises(Cancelled):
        run(fake, make_steps(order), Recorder(on_report))

    assert "destroy_vpn" not in order


def test_metrics_from_the_agent_may_carry_their_own_phase():
    fake = FakeAgent(phases=(("cutover_ready", 70),))
    original = fake.job
    fake.job = lambda advance=False: {**original(advance), "metrics": {"phase": "cutover_ready", "mismatch": 0}}
    seen = []

    def on_report(progress):
        if progress == 70:
            seen.append(progress)
            fake.state = "aborted"

    assert run(fake, make_steps([]), Recorder(on_report))["state"] == "stopped"
    assert seen == [70]


def test_app_deploy_signal_comes_once_even_when_streaming_is_skipped(caplog):
    for phases in ((("snapshot", 20), ("streaming", 50), ("cutover_ready", 70)),
                   (("snapshot", 20), ("cutover_ready", 70))):
        fake = FakeAgent(phases=phases)
        seen = []

        class PhaseRecorder:
            def report(self, state, progress, metrics=None, message=None):
                seen.append((metrics or {}).get("phase"))
                if progress == 70:
                    fake.state = "aborted"

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.features.db_migration"):
            run(fake, make_steps([]), PhaseRecorder())

        assert caplog.text.count("app.deploy_ready") == 1
        assert seen[-1] == "cutover_ready" and "snapshot" in seen


def test_stop_while_building_the_vpn_removes_it_without_registering_a_job():
    fake, order, ctx = FakeAgent(), [], Recorder()
    stopped = []
    steps = replace(make_steps(order),
                    ensure_vpn=lambda job_id, source, target: (order.append("ensure_vpn"), stopped.append(True)))

    result = run_migration(RUN_ID, SOURCE, TARGET, agent=fake.client(), steps=steps, ctx=ctx, poll_s=0,
                           log_interval_s=0, stop_requested=lambda: bool(stopped))

    assert order == ["ensure_vpn", "destroy_vpn"]
    assert "POST /jobs" not in fake.calls
    assert result["state"] == "stopped" and result["vpn_destroyed"] is True


def test_stop_during_probe_removes_the_vpn():
    fake, order = FakeAgent(), []
    stopped = []
    steps = replace(make_steps(order), probe=lambda source, target: (order.append("probe"), stopped.append(True)))

    result = run_migration(RUN_ID, SOURCE, TARGET, agent=fake.client(), steps=steps, poll_s=0,
                           log_interval_s=0, stop_requested=lambda: bool(stopped))

    assert order == ["ensure_vpn", "probe", "destroy_vpn"]
    assert result["state"] == "stopped"


def test_stop_just_before_the_job_is_registered_aborts_it():
    fake, order = FakeAgent(), []
    stopped = []
    steps = replace(make_steps(order), deliver_secrets=lambda job_id, source, target: (
        order.append("deliver_secrets"), stopped.append(True)))

    result = run_migration(RUN_ID, SOURCE, TARGET, agent=fake.client(), steps=steps, poll_s=0,
                           log_interval_s=0, stop_requested=lambda: bool(stopped))

    assert f"POST /jobs/{JOB_ID}/abort" in fake.calls
    assert order[-1] == "destroy_vpn" and result["state"] == "stopped"


def test_vpn_failure_after_stop_still_cleans_up_what_was_built():
    fake, order = FakeAgent(), []

    def broken(job_id, source, target):
        raise RuntimeError("terraform failed")

    steps = replace(make_steps(order), ensure_vpn=broken)
    result = run_migration(RUN_ID, SOURCE, TARGET, agent=fake.client(), steps=steps, poll_s=0,
                           log_interval_s=0, stop_requested=lambda: True)
    assert order == ["destroy_vpn"] and result["state"] == "stopped"


def test_vpn_failure_without_stop_keeps_it_for_a_rerun():
    fake, order = FakeAgent(), []

    def broken(job_id, source, target):
        raise RuntimeError("terraform failed")

    steps = replace(make_steps(order), ensure_vpn=broken)
    with pytest.raises(RuntimeError):
        run_migration(RUN_ID, SOURCE, TARGET, agent=fake.client(), steps=steps, poll_s=0, log_interval_s=0)
    assert order == []
