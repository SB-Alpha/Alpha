"""run and stop as workflows calls them, with the agent and VPN replaced by fakes."""

import copy
import json
import logging
import threading

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.secrets import delete_secret, has_secret, put_secret, resolve
from app.features import db_migration
from app.features.db_migration import db_workflow, service, vpn
from app.features.db_migration.client import AgentSettings, CdcAgentClient
from app.features.db_migration.input_secrets import INPUT_SECRETS

from .test_db_migration_vpn import INPUT, JOB_ID, RUN_ID

ADMIN_PASSWORD = "admin-password-marker"
SINK_PASSWORD = "sink-password-marker"
OCI_ENV = {
    "CDC_OCI_REGION": "oci-1", "CDC_OCI_COMPARTMENT_ID": "c", "CDC_OCI_DRG_ID": "d",
    "CDC_TFSTATE_BUCKET": "b", "CDC_TFSTATE_NAMESPACE": "n",
    "CDC_OCI_VCN_CIDR": "10.250.0.0/16", "CDC_NODE_SUBNET_CIDR": "10.250.1.0/24",
}


class FakeAgent:
    def __init__(self):
        self.job = None
        self.calls: list[str] = []
        self.secrets = None
        self.request = None
        self.abort_status = None
        self.reachable = True
        self.health = {"kafka": True, "connect": True, "plugins_missing": []}

    def client(self) -> CdcAgentClient:
        return CdcAgentClient(AgentSettings(url="https://cdc.example"), transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append(f"{method} {path}")
        if method == "GET" and path == "/status":
            return httpx.Response(200, json=self.health)
        if method == "POST" and path == "/probe":
            body = json.loads(request.content)
            return httpx.Response(200, json=[{**e, "ok": self.reachable, "error": None} for e in body])
        if method == "PUT" and path.endswith("/secrets"):
            self.secrets = json.loads(request.content)
            return httpx.Response(200, json={"stored": True})
        if method == "POST" and path == "/jobs":
            self.request = json.loads(request.content)
            self.job = self.job or {"job_id": JOB_ID, "state": "running", "phase": "cutover_ready", "progress": 70,
                                    "message": "", "metrics": {"phase": "cutover_ready", "mismatch": 0},
                                    "outputs": {"schema_created": ["public.work_items"]}, "evidence": {}}
            return httpx.Response(202, json=self.job)
        if self.job is None:
            return httpx.Response(404, json={"detail": "not found"})
        if path.endswith("/abort"):
            if self.abort_status:
                return httpx.Response(self.abort_status, json={"detail": "cleanup failed"})
            self.job = {**self.job, "state": "aborted", "evidence": {"cleanup": {"slot_dropped": True}}}
            return httpx.Response(200, json=self.job)
        if path.endswith("/retry"):
            self.job = {**self.job, "state": "running"}
            return httpx.Response(202, json=self.job)
        return httpx.Response(200, json=self.job)


class Recorder:
    def __init__(self):
        self.reports = []

    def report(self, state, progress, metrics=None, message=None):
        self.reports.append((state, progress, message))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db_workflow, "_stop_requested", set())
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    for name, value in OCI_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(db_workflow.time, "sleep", lambda s: None)
    put_secret(f"{RUN_ID}/source-cloud", {"aws_access_key_id": "k", "aws_secret_access_key": "s"})
    put_secret(f"{RUN_ID}/target-cloud", {"credentials_json": "{}"})
    put_secret(f"{RUN_ID}/source-db-admin", {"user": "alpha", "password": ADMIN_PASSWORD})
    put_secret(f"{RUN_ID}/target-admin", {"user": "postgres", "password": "target-admin-pw"})
    put_secret(f"{RUN_ID}/target-ddl-user", {"user": "alpha_ddl", "password": "ddl-pw"})
    put_secret(f"{RUN_ID}/target-sink-user", {"user": "alpha_sink", "password": SINK_PASSWORD})


@pytest.fixture
def vpn_calls(monkeypatch):
    calls: list[str] = []

    def ensure(mig, job_id):
        calls.append("ensure_vpn")
        return {"tunnel_status": {}}

    monkeypatch.setattr(vpn, "ensure_vpn", ensure)
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: calls.append("destroy_vpn") or True)
    return calls


def start(agent: FakeAgent, inp=INPUT, ctx=None):
    """Run in the background as workflows does; returns the thread and a holder for the result."""
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(result=service.run(inp, ctx, agent=agent.client())))
    thread.start()
    return thread, holder


def wait_created(agent, thread):
    while agent.job is None and thread.is_alive():
        thread.join(0.01)


def test_public_names():
    assert db_migration.run is service.run and db_migration.stop is service.stop
    assert db_migration.complete is service.complete


def inputs_left() -> list[str]:
    return [tag for tag in INPUT_SECRETS if has_secret(f"secret://{RUN_ID}/{tag}")]


def test_complete_removes_the_inputs_only_after_a_live_run_removed_the_vpn(monkeypatch, vpn_calls):
    seen = {}

    def destroy(run_id):
        threading.Event().wait(0.2)  # complete must still be waiting
        seen["cloud_key"] = resolve(f"secret://{RUN_ID}/source-cloud")

    monkeypatch.setattr(vpn, "destroy_vpn", destroy)
    agent = FakeAgent()
    thread, holder = start(agent)
    wait_created(agent, thread)

    result = service.complete(RUN_ID, agent=agent.client())
    thread.join(5)

    assert seen["cloud_key"]["aws_access_key_id"] == "k"
    assert holder["result"].vpn == {"destroyed": True, "error": None}
    assert result["secrets_removed"] == sorted(INPUT_SECRETS)
    assert inputs_left() == []


def test_complete_with_no_live_run_removes_the_vpn_then_the_inputs(vpn_calls):
    agent = FakeAgent()

    service.complete(RUN_ID, agent=agent.client())

    assert vpn_calls == ["destroy_vpn"]
    assert inputs_left() == []


def test_complete_keeps_the_inputs_when_cleanup_fails_and_can_be_called_again(vpn_calls):
    agent = FakeAgent()
    agent.job = {"job_id": JOB_ID, "state": "running", "phase": "cutover_ready", "progress": 70, "message": "",
                 "metrics": {}, "outputs": {}, "evidence": {}}
    agent.abort_status = 502

    with pytest.raises(Exception, match="cleanup failed"):
        service.complete(RUN_ID, agent=agent.client())
    assert len(inputs_left()) == len(INPUT_SECRETS)

    agent.abort_status = None
    service.complete(RUN_ID, agent=agent.client())
    assert inputs_left() == []


def test_complete_keeps_the_inputs_when_a_live_run_could_not_remove_the_vpn(monkeypatch, vpn_calls):
    def destroy(run_id):
        raise RuntimeError("terraform failed")

    monkeypatch.setattr(vpn, "destroy_vpn", destroy)
    put_secret(f"{RUN_ID}/vpn-context", {"json": "{}"})
    agent = FakeAgent()
    thread, _ = start(agent)
    wait_created(agent, thread)

    with pytest.raises(RuntimeError, match="not removed"):
        service.complete(RUN_ID, agent=agent.client())
    thread.join(5)

    assert len(inputs_left()) == len(INPUT_SECRETS)


def test_complete_keeps_the_inputs_when_the_run_does_not_end_in_time(monkeypatch, vpn_calls):
    monkeypatch.setenv("CDC_COMPLETE_WAIT_S", "-1")
    monkeypatch.setattr(db_workflow, "_active", {RUN_ID})

    with pytest.raises(RuntimeError, match="call complete again"):
        service.complete(RUN_ID, agent=FakeAgent().client())

    assert len(inputs_left()) == len(INPUT_SECRETS)


def test_run_builds_then_stop_cleans_up(vpn_calls):
    agent, ctx = FakeAgent(), Recorder()
    thread, holder = start(agent, ctx=ctx)
    wait_created(agent, thread)
    assert service.stop(RUN_ID, agent=agent.client())["job_id"] == JOB_ID
    thread.join(5)
    result = holder["result"]

    assert vpn_calls == ["ensure_vpn", "destroy_vpn"]
    assert agent.calls.index("POST /probe") < agent.calls.index(f"PUT /jobs/{JOB_ID}/secrets") \
        < agent.calls.index("POST /jobs")
    assert result.state == "stopped" and result.vpn == {"destroyed": True, "error": None}
    assert result.evidence["cleanup"]["slot_dropped"] is True
    assert ctx.reports[-1][:2] == ("stopped", 100)


def test_job_gets_the_app_role_and_secrets_get_all_accounts(vpn_calls):
    agent = FakeAgent()
    thread, _ = start(agent)
    wait_created(agent, thread)
    service.stop(RUN_ID, agent=agent.client())
    thread.join(5)

    assert agent.request["target"]["app_role"] == "app_user"
    assert agent.request["target"]["host"] == "10.20.0.5"
    assert agent.secrets["source.admin.password"] == ADMIN_PASSWORD
    assert agent.secrets["target.password"] == SINK_PASSWORD
    assert agent.secrets["source.user"] == "alpha_cdc_repl"
    assert len(agent.secrets["source.password"]) == 48


def test_generated_secrets_go_after_the_vpn_and_inputs_stay(monkeypatch):
    seen = {}

    def destroy(run_id):
        seen["repl_during_destroy"] = resolve(f"secret://{run_id}/source-repl")
        return True

    monkeypatch.setattr(vpn, "ensure_vpn", lambda mig, job_id: {"tunnel_status": {}})
    monkeypatch.setattr(vpn, "destroy_vpn", destroy)
    agent = FakeAgent()
    thread, _ = start(agent)
    wait_created(agent, thread)
    service.stop(RUN_ID, agent=agent.client())
    thread.join(5)

    assert seen["repl_during_destroy"]["user"] == "alpha_cdc_repl"
    with pytest.raises(KeyError):
        resolve(f"secret://{RUN_ID}/source-repl")
    assert resolve(f"secret://{RUN_ID}/source-db-admin")["user"] == "alpha"


def test_missing_input_fails_without_touching_anything(vpn_calls, caplog):
    agent, ctx = FakeAgent(), Recorder()
    bad = {k: v for k, v in INPUT.items() if k != "target"}
    with caplog.at_level(logging.INFO, logger="app.features.db_migration"):
        result = service.run(bad, ctx, agent=agent.client())

    assert result.state == "failed" and "target" in result.message
    assert vpn_calls == [] and agent.calls == []
    assert ctx.reports[-1][0] == "failed"
    assert "inputs.missing" in caplog.text


def test_invalid_value_is_not_quoted_in_the_result(vpn_calls):
    bad = {**INPUT, "source": {**INPUT["source"], "credentials_ref": {"cloud": "x", "db_admin": 12345678}}}
    result = service.run(bad, agent=FakeAgent().client())

    assert result.state == "failed" and "12345678" not in result.message
    assert "source.credentials_ref.db_admin" in result.message


def test_unknown_secret_reference_fails_before_the_vpn(vpn_calls):
    agent = FakeAgent()
    inp = json.loads(json.dumps(INPUT))
    inp["target"]["credentials_ref"]["sink_user"] = "secret://mig/none"
    result = service.run(inp, agent=agent.client())

    assert result.state == "failed" and "secret://mig/none" in result.message
    assert vpn_calls == [] and agent.calls == []


def test_unreachable_database_fails_and_keeps_the_vpn(vpn_calls, monkeypatch):
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "-1")
    agent = FakeAgent()
    agent.reachable = False
    result = service.run(INPUT, agent=agent.client())

    assert result.state == "failed" and "rds.example:5432" in result.message
    assert vpn_calls == ["ensure_vpn"]
    assert "POST /jobs" not in agent.calls


def test_failed_job_is_reported_and_keeps_the_vpn(vpn_calls):
    agent = FakeAgent()
    agent.job = {"job_id": JOB_ID, "state": "failed", "phase": "precheck", "progress": 2, "message": "precheck 실패",
                 "metrics": {}, "outputs": {}, "evidence": {}}
    calls = []
    original = agent.handle

    def stay_failed(request):
        response = original(request)
        if request.url.path.endswith("/retry"):
            calls.append("retry")
            agent.job = {**agent.job, "state": "failed"}
        return response

    agent.handle = stay_failed
    result = service.run(INPUT, agent=agent.client())

    assert calls == ["retry"]  # the same run_id continues from the failed step
    assert result.state == "failed" and "precheck" in result.message
    assert vpn_calls == ["ensure_vpn"]


def test_stop_after_a_failed_run_removes_the_vpn_itself(vpn_calls, monkeypatch):
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "-1")
    agent = FakeAgent()
    agent.reachable = False
    service.run(INPUT, agent=agent.client())
    service.stop(RUN_ID, agent=agent.client())  # no job on the agent yet: 404 is fine

    assert vpn_calls == ["ensure_vpn", "destroy_vpn"]


def test_failed_cleanup_keeps_the_vpn_so_the_slot_can_still_be_removed(vpn_calls):
    agent = FakeAgent()
    agent.job = {"job_id": JOB_ID, "state": "running", "phase": "cutover_ready", "progress": 70, "message": "",
                 "metrics": {}, "outputs": {}, "evidence": {}}
    agent.abort_status = 502
    with pytest.raises(Exception, match="cleanup failed"):
        service.stop(RUN_ID, agent=agent.client())

    assert vpn_calls == []
    assert resolve(f"secret://{RUN_ID}/source-cloud")


def test_finished_run_is_not_started_again(vpn_calls):
    agent = FakeAgent()
    agent.job = {"job_id": JOB_ID, "state": "aborted", "phase": "cutover_ready", "progress": 70, "message": "",
                 "metrics": {}, "outputs": {}, "evidence": {}}
    result = service.run(INPUT, agent=agent.client())

    assert result.state == "failed" and "new run_id" in result.message
    assert vpn_calls == [] and agent.secrets is None


def test_skip_vpn_needs_no_cloud_keys(vpn_calls):
    inp = json.loads(json.dumps(INPUT))
    inp["options"] = {"skip_vpn": True}
    inp["source"]["credentials_ref"]["cloud"] = "secret://none/none"
    inp["target"]["credentials_ref"]["cloud"] = "secret://none/none"
    agent = FakeAgent()
    thread, holder = start(agent, inp)
    wait_created(agent, thread)
    service.stop(RUN_ID, agent=agent.client())
    thread.join(5)

    assert holder["result"].state == "stopped"
    assert "ensure_vpn" not in vpn_calls


def test_logs_are_json_lines_without_secret_values(vpn_calls, caplog):
    agent = FakeAgent()
    with caplog.at_level(logging.INFO, logger="app.features.db_migration"):
        thread, _ = start(agent)
        wait_created(agent, thread)
        service.stop(RUN_ID, agent=agent.client())
        thread.join(5)
    lines = [json.loads(r.message) for r in caplog.records if r.name == "app.features.db_migration"]
    events = [line["event"] for line in lines]

    assert all(line["run_id"] == RUN_ID and line["job_id"] == JOB_ID and line["ts"] for line in lines)
    for expected in ("inputs.loaded", "run.start", "vpn.start", "vpn.ready", "probe", "job.created", "phase",
                     "metrics", "run.stopped"):
        assert expected in events
    assert ADMIN_PASSWORD not in caplog.text and SINK_PASSWORD not in caplog.text


def without_account_refs() -> dict:
    inp = copy.deepcopy(INPUT)
    del inp["target"]["credentials_ref"]["ddl_user"], inp["target"]["credentials_ref"]["sink_user"]
    return inp


def run_until_stopped(agent, inp=INPUT):
    thread, holder = start(agent, inp)
    wait_created(agent, thread)
    service.stop(RUN_ID, agent=agent.client())
    thread.join(5)
    return holder["result"]


def test_target_admin_reaches_the_agent_and_saved_accounts_are_used(vpn_calls):
    agent = FakeAgent()
    run_until_stopped(agent)

    assert agent.secrets["target.admin.user"] == "postgres"
    assert agent.secrets["target.admin.password"] == "target-admin-pw"
    assert agent.secrets["target.ddl.password"] == "ddl-pw"
    assert agent.secrets["target.password"] == SINK_PASSWORD


def test_accounts_without_a_reference_are_made_here_and_kept_for_a_retry(vpn_calls):
    delete_secret(f"{RUN_ID}/target-ddl-user")
    delete_secret(f"{RUN_ID}/target-sink-user")
    agent = FakeAgent()
    run_until_stopped(agent, without_account_refs())

    assert agent.secrets["target.ddl.user"] == "alpha_ddl" and agent.secrets["target.user"] == "alpha_sink"
    assert agent.secrets["target.ddl.password"] != agent.secrets["target.password"]
    saved = resolve(f"secret://{RUN_ID}/target-ddl-user")
    assert saved["password"] == agent.secrets["target.ddl.password"]

    again = service.resolve_inputs(service.DbMigrationInput.model_validate(without_account_refs()))
    assert again.target_ddl == saved


def test_accounts_without_a_reference_use_what_was_saved_under_the_run(vpn_calls):
    agent = FakeAgent()
    run_until_stopped(agent, without_account_refs())

    assert agent.secrets["target.ddl.password"] == "ddl-pw"


def test_a_given_account_reference_that_is_missing_fails_before_the_vpn(vpn_calls):
    delete_secret(f"{RUN_ID}/target-ddl-user")
    result = service.run(INPUT, agent=FakeAgent().client())

    assert result.state == "failed" and "target-ddl-user" in result.message
    assert vpn_calls == []


def test_missing_admin_reference_is_refused_by_field_name(vpn_calls):
    inp = copy.deepcopy(INPUT)
    del inp["target"]["credentials_ref"]["admin"]
    result = service.run(inp, agent=FakeAgent().client())

    assert result.state == "failed" and "target.credentials_ref.admin" in result.message
    assert vpn_calls == []


def test_an_admin_that_is_not_the_app_account_is_refused_before_anything_is_built(vpn_calls):
    put_secret(f"{RUN_ID}/source-db-admin", {"user": "separate_admin", "password": ADMIN_PASSWORD})
    agent = FakeAgent()

    result = service.run(INPUT, agent=agent.client())

    assert result.state == "failed" and "not supported yet" in result.message
    assert "separate_admin" not in result.message
    assert vpn_calls == [] and agent.calls == []


def test_target_admin_password_stays_out_of_the_logs(vpn_calls, caplog):
    with caplog.at_level(logging.INFO, logger="app.features.db_migration"):
        run_until_stopped(FakeAgent())

    assert "target-admin-pw" not in caplog.text


def test_stop_while_the_vpn_is_being_built_ends_the_run(monkeypatch):
    agent = FakeAgent()
    building, release, calls = threading.Event(), threading.Event(), []

    def ensure(mig, job_id):
        building.set()
        release.wait(5)
        calls.append("ensure_vpn")
        return {"tunnel_status": {}}

    monkeypatch.setattr(vpn, "ensure_vpn", ensure)
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: calls.append("destroy_vpn") or True)
    thread, holder = start(agent)
    assert building.wait(5)

    assert service.stop(RUN_ID, agent=agent.client())["vpn"] == "removed by the run"
    release.set()
    thread.join(5)

    assert calls == ["ensure_vpn", "destroy_vpn"]
    assert "POST /jobs" not in agent.calls
    assert holder["result"].state == "stopped"
    assert not db_workflow._stop_requested


def test_a_failed_stop_does_not_make_the_run_remove_the_vpn(monkeypatch, vpn_calls):
    agent = FakeAgent()
    thread, holder = start(agent)
    wait_created(agent, thread)
    agent.abort_status = 502
    with pytest.raises(Exception):
        service.stop(RUN_ID, agent=agent.client())
    assert not db_workflow._stop_requested
    assert vpn_calls == ["ensure_vpn"]

    agent.abort_status = None
    service.stop(RUN_ID, agent=agent.client())
    thread.join(5)
    assert vpn_calls == ["ensure_vpn", "destroy_vpn"]


# ---- before the VPN: it takes 15-20 minutes to build


@pytest.fixture
def vpn_not_reached(monkeypatch):
    """Fails the run at once if a check let it through, instead of following a job that never ends."""

    def ensure(mig, job_id):
        raise AssertionError("the VPN must not be built")

    monkeypatch.setattr(vpn, "ensure_vpn", ensure)


def test_broken_cluster_stops_the_run_before_the_vpn(vpn_not_reached):
    agent = FakeAgent()
    agent.health = {"kafka": True, "connect": False, "plugins_missing": ["debezium"]}

    result = service.run(INPUT, agent=agent.client())

    assert result.state == "failed"
    assert "connect" in result.message and "debezium" in result.message


def test_overlapping_ranges_stop_the_run_before_the_vpn(vpn_not_reached, monkeypatch):
    source_cidr = copy.deepcopy(INPUT)["source"]["network"]["cidr"]
    monkeypatch.setenv("CDC_OCI_VCN_CIDR", source_cidr)
    monkeypatch.setenv("CDC_NODE_SUBNET_CIDR", source_cidr)

    result = service.run(INPUT, agent=FakeAgent().client())

    assert result.state == "failed" and "overlap" in result.message


def test_missing_oci_setting_is_named_before_the_vpn(vpn_not_reached, monkeypatch):
    monkeypatch.delenv("CDC_OCI_VCN_CIDR")

    result = service.run(INPUT, agent=FakeAgent().client())

    assert result.state == "failed" and "CDC_OCI_VCN_CIDR" in result.message


def test_checks_for_the_vpn_are_skipped_when_no_vpn_is_built(vpn_calls, monkeypatch):
    monkeypatch.delenv("CDC_OCI_VCN_CIDR")
    agent = FakeAgent()
    agent.health = {"kafka": False, "connect": False}
    inp = {**copy.deepcopy(INPUT), "options": {"skip_vpn": True}}

    thread, _ = start(agent, inp)
    wait_created(agent, thread)
    agent.job = {**agent.job, "state": "aborted"}
    thread.join()

    assert "GET /status" not in agent.calls
    assert agent.request is not None
