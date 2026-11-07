"""The module's interface with the agent, VPN and source lookup replaced by fakes."""

import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.secrets import has_secret, put_secret
from app.features.db_migration import cloud_lookup, db_workflow, vpn, vpn_workflow
from app.features.db_migration import router as db_router
from app.features.db_migration.client import AgentSettings, CdcAgentClient
from app.features.db_migration.input_secrets import INPUT_SECRETS
from app.features.db_migration.settings import MissingSetting, tunables
from app.main import app

from .test_db_migration_vpn import INPUT, JOB_ID, RUN_ID

ACCESS_KEY = "AKIAABCDEFGHIJKLMNOP"
SECRET_KEY = "secret-key-marker-value"
DB_PASSWORD = "db-password-marker"
START = {
    "run_id": RUN_ID,
    "source": {"cloud": "aws", "region": "ap-northeast-2", "db": {"user": "alpha", "password": DB_PASSWORD}},
    "target": {"cloud": "gcp", "region": "asia-northeast1",
               "db": {"user": "postgres", "password": "target-admin-pw", "app_user": "app_user"}},
}
READY = {"state": "waiting_approval", "phase": "cutover_ready", "progress": 70}
SWITCHED = {"state": "running", "phase": "stabilizing", "progress": 90}
OTHER_JOB = {"job_id": "other", "state": "running", "phase": "streaming",
             "request": {"source": {"host": "rds.example", "database": "postgres"}}}


class FakeAgent:
    def __init__(self):
        self.job = None
        self.calls: list[str] = []
        self.health = {"kafka": True, "connect": True, "plugins_missing": [], "worker_configured": True}
        self.other_jobs: list[dict] = []
        self.on_cutover = lambda job: {**job, **SWITCHED}
        self.cutover_error = None
        self.close_status = None
        self.abort_status = None
        self.cleanup = {"slot_dropped": True, "target_accounts": {"retired": ["alpha_sink", "alpha_ddl"]}}
        self.down = False

    def client(self) -> CdcAgentClient:
        return CdcAgentClient(AgentSettings(url="https://cdc.example"), transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        method, path = request.method, request.url.path
        self.calls.append(f"{method} {path}")
        if path == "/status":
            return httpx.Response(200, json=self.health)
        if path == "/catalog":
            query = json.loads(request.content)
            if query["database"] is None:
                return httpx.Response(200, json={"database": None, "tables": [],
                                                 "databases": ["postgres" if query["host"] == "rds.example" else "app"]})
            tables = [{"schema_name": "public", "name": "work_items", "primary_key": ["id"], "kind": "table",
                       "is_partition": False, "can_select": True}]
            return httpx.Response(200, json={"database": query["database"], "databases": [], "tables": tables})
        if path == "/probe":
            return httpx.Response(200, json=[{**e, "ok": True, "error": None} for e in json.loads(request.content)])
        if path.endswith("/secrets"):
            return httpx.Response(200, json={"stored": True})
        if method == "GET" and path == "/jobs":
            return httpx.Response(200, json=self.other_jobs + ([self.job] if self.job else []))
        if method == "POST" and path == "/jobs":
            self.job = self.job or {"job_id": JOB_ID, "message": "", "metrics": {"mismatch": 0}, "outputs": {},
                                    "evidence": {}, "request": {"source": {"host": "rds.example", "database": "postgres"}},
                                    **READY}
            return httpx.Response(202, json=self.job)
        if self.job is None:
            return httpx.Response(404, json={"detail": "not found"})
        if path.endswith("/cutover"):
            if self.cutover_error:
                return httpx.Response(self.cutover_error[0], json={"detail": self.cutover_error[1]})
            self.job = self.on_cutover(self.job)
            return httpx.Response(202, json=self.job)
        if path.endswith("/close"):
            if self.close_status:
                return httpx.Response(self.close_status, json={"detail": "cleanup failed"})
            self.job = {**self.job, "state": "succeeded", "phase": "closed", "progress": 100,
                        "evidence": {**self.job["evidence"], "cleanup": self.cleanup}}
            return httpx.Response(200, json=self.job)
        if path.endswith("/abort"):
            if self.abort_status:
                return httpx.Response(self.abort_status, json={"detail": "cleanup failed"})
            if self.job["phase"] in ("cutover", "stabilizing", "closed"):
                return httpx.Response(409, json={"detail": "abort is allowed only before cutover"})
            self.job = {**self.job, "state": "aborted", "evidence": {"cleanup": self.cleanup}}
            return httpx.Response(200, json=self.job)
        return httpx.Response(200, json=self.job)


def found_endpoint(cloud, region, host, cidrs, publicly_accessible=False):
    db = cloud_lookup.FoundDb("db", host, 5432, "16", publicly_accessible, True)
    return cloud_lookup.FoundEndpoint(cloud, region, "scope", db, cidrs, {})


ROUTE = cloud_lookup.FoundRoute(
    source=found_endpoint("aws", "ap-northeast-2", "rds.example", ("172.31.0.0/16",)),
    target=found_endpoint("gcp", "asia-northeast1", "10.20.0.5", ("10.10.0.0/24", "10.20.0.0/20")),
)


@pytest.fixture(autouse=True)
def _cloud_lookup(monkeypatch):
    monkeypatch.setattr(cloud_lookup, "find_route", lambda injected: ROUTE)


@pytest.fixture
def fake():
    return FakeAgent()


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    for name, value in {"CDC_OCI_REGION": "oci-1", "CDC_OCI_COMPARTMENT_ID": "c", "CDC_OCI_DRG_ID": "d",
                        "CDC_TFSTATE_BUCKET": "b", "CDC_TFSTATE_NAMESPACE": "n", "CDC_OCI_VCN_CIDR": "10.50.0.0/16",
                        "CDC_NODE_SUBNET_CIDR": "10.50.1.0/24", "CDC_LOG_INTERVAL_S": "0.01", "CDC_JOB_POLL_S": "0.01",
                        "CDC_CUTOVER_POLL_S": "0.01", "CDC_COMPLETE_POLL_S": "0.01"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(db_workflow, "_runs", {})
    put_secret("cloud/aws", {"aws_access_key_id": ACCESS_KEY, "aws_secret_access_key": SECRET_KEY})
    put_secret("cloud/gcp", {"credentials_json": "{}"})
    put_secret(f"{RUN_ID}/target-cloud", {"credentials_json": "{}"})
    put_secret(f"{RUN_ID}/target-admin", {"user": "postgres", "password": "target-admin-pw"})
    put_secret(f"{RUN_ID}/target-ddl-user", {"user": "alpha_ddl", "password": "ddl-pw"})
    put_secret(f"{RUN_ID}/target-sink-user", {"user": "alpha_sink", "password": "sink-pw"})
    yield
    # Background runs must end before this test's folder and fakes go away.
    fake.down = False
    if fake.job:
        fake.job = {**fake.job, "state": "aborted"}
    deadline = time.monotonic() + 5
    while db_workflow._active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not db_workflow._active


@pytest.fixture
def vpn_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(vpn, "ensure_vpn", lambda mig, job_id: calls.append("ensure_vpn") or {"tunnel_status": {}})
    monkeypatch.setattr(vpn_workflow, "ensure_vpn", lambda plan: calls.append("ensure_vpn") or {"tunnel_status": {}})
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: calls.append("destroy_vpn") or True)
    return calls


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def wait_ended():
    wait_until(lambda: RUN_ID not in db_workflow._active)


def start_to_ready(fake, **changes):
    run_id = db_workflow.run({**START, **changes}, agent=fake.client())
    wait_until(lambda: db_workflow.status(run_id, agent=fake.client()).ready_for_cutover)
    return run_id


def failed_status(fake, **changes):
    db_workflow.run({**START, **changes}, agent=fake.client())
    wait_ended()
    return db_workflow.status(RUN_ID, agent=fake.client())


def use_route(monkeypatch, *, public=False, source_cidrs=("172.31.0.0/16",), target_cidrs=("10.10.0.0/24",)):
    route = cloud_lookup.FoundRoute(
        source=found_endpoint("aws", "ap-northeast-2", "rds.example", source_cidrs, public),
        target=found_endpoint("gcp", "asia-northeast1", "10.20.0.5", target_cidrs),
    )
    monkeypatch.setattr(cloud_lookup, "find_route", lambda injected: route)


# ---- run


def test_run_returns_at_once_and_goes_in_order_to_cutover_ready(fake, vpn_calls, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(cloud_lookup, "find_route", lambda injected: gate.wait(5) and ROUTE)

    run_id = db_workflow.run(START, agent=fake.client())

    # 끝난 run인지 확인하는 호출 하나뿐이고, 나머지는 백그라운드에서 진행한다.
    assert run_id == RUN_ID and fake.calls == [f"GET /jobs/{JOB_ID}"]
    assert db_workflow.status(run_id, agent=fake.client()).state == "running"
    gate.set()
    wait_until(lambda: db_workflow.status(run_id, agent=fake.client()).ready_for_cutover)
    assert vpn_calls == ["ensure_vpn"]
    assert (fake.calls.index("GET /jobs") < fake.calls.index("POST /probe")
            < fake.calls.index(f"PUT /jobs/{JOB_ID}/secrets") < fake.calls.index("POST /jobs"))


def test_agent_is_checked_before_the_vpn(fake, vpn_calls):
    fake.health = {**fake.health, "kafka": False, "plugins_missing": ["io.debezium.x"]}

    status = failed_status(fake)

    assert status.state == "failed" and status.phase == "inputs"
    assert "kafka" in status.message and "connector plugins" in status.message
    assert vpn_calls == []


def test_run_id_is_made_when_not_given(fake, vpn_calls):
    run_id = db_workflow.run({k: v for k, v in START.items() if k != "run_id"}, agent=fake.client())
    wait_until(lambda: run_id not in db_workflow._active or fake.job)
    assert run_id.startswith("mig-")
    db_workflow._stop_requested.add(run_id)


# ---- refusals before the VPN


@pytest.mark.parametrize("route, expected", [
    ({"public": True}, "public access"),
    ({"source_cidrs": ("10.50.0.0/24",)}, "overlap"),
    ({"source_cidrs": ("10.10.0.0/16",)}, "overlap"),
    ({"source_cidrs": ("not-a-cidr",)}, "not a CIDR"),
])
def test_refused_before_anything_is_built(fake, vpn_calls, monkeypatch, route, expected):
    use_route(monkeypatch, **route)

    status = failed_status(fake)

    assert status.state == "failed" and status.phase == "inputs" and expected in status.message
    assert vpn_calls == [] and "POST /jobs" not in fake.calls


def test_unknown_public_access_goes_on(fake, vpn_calls, monkeypatch):
    use_route(monkeypatch, public=None)
    start_to_ready(fake)
    assert vpn_calls == ["ensure_vpn"]


def test_another_job_on_the_same_source_is_refused(fake, vpn_calls):
    fake.other_jobs = [OTHER_JOB]

    status = failed_status(fake)

    assert "same source DB" in status.message and vpn_calls == []


def test_a_finished_job_on_the_same_source_is_no_obstacle(fake, vpn_calls):
    fake.other_jobs = [{**OTHER_JOB, "state": "succeeded"}]
    start_to_ready(fake)


def test_same_run_id_while_running_is_refused(fake, vpn_calls):
    start_to_ready(fake)
    with pytest.raises(db_workflow.NotAllowed, match="already running"):
        db_workflow.run(START, agent=fake.client())


def test_another_run_on_this_app_on_the_same_source_is_refused(fake, vpn_calls, monkeypatch):
    monkeypatch.setitem(db_workflow._runs, "other-run", db_workflow._Progress(stage="vpn", source=("rds.example", "postgres")))
    db_workflow._active.add("other-run")
    try:
        status = failed_status(fake)
    finally:
        db_workflow._active.discard("other-run")

    assert "same source DB" in status.message and vpn_calls == []


def test_missing_settings_name_the_variable(fake, vpn_calls, monkeypatch):
    monkeypatch.delenv("CDC_NODE_SUBNET_CIDR")

    status = failed_status(fake)

    assert status.phase == "inputs" and "CDC_NODE_SUBNET_CIDR" in status.message
    assert vpn_calls == []


def test_node_subnet_must_be_inside_the_vcn(fake, vpn_calls, monkeypatch):
    monkeypatch.setenv("CDC_NODE_SUBNET_CIDR", "10.60.1.0/24")
    assert "not inside" in failed_status(fake).message


# ---- status


def test_status_of_an_unknown_run(fake):
    with pytest.raises(db_workflow.UnknownRun):
        db_workflow.status("no-such-run", agent=fake.client())


def test_status_without_the_agent_says_unknown_and_lists_what_remains(fake, vpn_calls):
    start_to_ready(fake)
    fake.down = True

    status = db_workflow.status(RUN_ID, agent=fake.client())

    assert status.state == "unknown" and status.cleanup_required
    assert "secret:source-db-admin" in status.remaining


def test_failure_in_the_background_shows_the_stage_and_hides_secrets(fake, monkeypatch):
    def broken(plan):
        raise RuntimeError(f"apply failed: {SECRET_KEY} AKIAZZZZZZZZZZZZZZZZ password=hunter2 {DB_PASSWORD}")

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", broken)

    status = failed_status(fake)

    assert status.state == "failed" and status.phase == "vpn" and status.cleanup_required
    for hidden in (SECRET_KEY, "AKIAZZZZZZZZZZZZZZZZ", "hunter2", DB_PASSWORD):
        assert hidden not in status.message
    assert "같은 run_id로 다시 실행" in status.message


def test_status_and_logs_carry_no_secret(fake, vpn_calls, caplog):
    with caplog.at_level(logging.INFO):
        run_id = start_to_ready(fake)
    shown = db_workflow.status(run_id, agent=fake.client()).model_dump_json() + caplog.text

    for hidden in (ACCESS_KEY, SECRET_KEY, DB_PASSWORD):
        assert hidden not in shown


def test_run_ends_at_stabilizing_and_keeps_the_vpn(fake, vpn_calls):
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())
    wait_ended()

    assert vpn_calls == ["ensure_vpn"]
    assert db_workflow.status(RUN_ID, agent=fake.client()).phase == "stabilizing"


# ---- cutover


def steps(*names):
    return {"cutover": {"steps": [{"step": name} for name in names], "hold_ms": 120.0}}


def test_cutover_only_when_waiting_for_it(fake, vpn_calls):
    fake.job = {"job_id": JOB_ID, "state": "running", "phase": "streaming", "progress": 50, "message": "",
                "metrics": {}, "outputs": {}, "evidence": {}}
    with pytest.raises(db_workflow.NotAllowed, match="cutover_ready"):
        db_workflow.cutover(RUN_ID, "pm", agent=fake.client())
    assert f"POST /jobs/{JOB_ID}/cutover" not in fake.calls


def test_cutover_needs_who_approved_it(fake):
    with pytest.raises(db_workflow.InvalidInput):
        db_workflow.cutover(RUN_ID, "  ", agent=fake.client())


def test_cutover_of_an_unknown_run(fake):
    with pytest.raises(db_workflow.UnknownRun):
        db_workflow.cutover(RUN_ID, "pm", agent=fake.client())


def test_cutover_success(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, **SWITCHED, "evidence": steps("hold", "switched_to_target", "released")}
    start_to_ready(fake)

    result = db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    assert result.outcome == "switched" and result.hold_ms == 120.0


def test_cutover_returned_to_source_when_the_caller_confirms_it(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, **READY, "evidence": steps("hold", "failed", "released_to_source")}
    start_to_ready(fake)

    result = db_workflow.cutover(RUN_ID, "pm", agent=fake.client(), confirm_source=lambda: True)

    assert result.outcome == "rolled_back" and result.metrics["cutover_outcome"] == "rolled_back"


def test_reported_return_to_source_is_unknown_without_a_confirmation(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, **READY, "evidence": steps("hold", "failed", "released_to_source")}
    start_to_ready(fake)

    assert db_workflow.cutover(RUN_ID, "pm", agent=fake.client()).outcome == "unknown"


def test_cutover_that_could_not_undo_its_block_needs_a_person(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, **READY, "evidence": steps("failed", "source_writes_unblocked_failed")}
    start_to_ready(fake)

    assert db_workflow.cutover(RUN_ID, "pm", agent=fake.client()).outcome == "unknown"


def test_failure_after_the_switch_needs_a_person(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, "state": "failed", "phase": "cutover",
                                   "evidence": steps("switched_to_target", "failed")}
    start_to_ready(fake)

    assert db_workflow.cutover(RUN_ID, "pm", agent=fake.client()).outcome == "post_switch_failed"


def test_cutover_past_the_wait_says_look_again(fake, vpn_calls, monkeypatch):
    monkeypatch.setenv("CDC_CUTOVER_WAIT_S", "-1")
    fake.on_cutover = lambda job: {**job, "state": "running", "phase": "cutover", "evidence": steps("hold")}
    start_to_ready(fake)

    result = db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    assert result.outcome == "unknown" and result.phase == "cutover"


def test_second_call_during_a_cutover_does_not_start_another(fake, vpn_calls, monkeypatch):
    monkeypatch.setenv("CDC_CUTOVER_WAIT_S", "-1")
    fake.on_cutover = lambda job: {**job, "state": "running", "phase": "cutover", "evidence": steps("hold")}
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())
    fake.job = {**fake.job, **SWITCHED}

    result = db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    assert result.outcome == "switched"
    assert fake.calls.count(f"POST /jobs/{JOB_ID}/cutover") == 1


def test_call_after_the_switch_returns_the_result(fake, vpn_calls):
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    assert db_workflow.cutover(RUN_ID, "pm", agent=fake.client()).outcome == "switched"
    assert fake.calls.count(f"POST /jobs/{JOB_ID}/cutover") == 1


def test_simultaneous_calls_share_one_cutover(fake, vpn_calls):
    gate = threading.Event()

    def slow(job):
        gate.wait(5)
        return {**job, **SWITCHED}

    fake.on_cutover = slow
    start_to_ready(fake)
    results = []
    threads = [threading.Thread(target=lambda: results.append(db_workflow.cutover(RUN_ID, "pm", agent=fake.client())))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    wait_until(lambda: RUN_ID in db_workflow._cutovers)
    time.sleep(0.05)
    gate.set()
    for thread in threads:
        thread.join(5)

    assert [r.outcome for r in results] == ["switched", "switched"]
    assert fake.calls.count(f"POST /jobs/{JOB_ID}/cutover") == 1


def test_worker_not_ready_is_refused_and_the_job_waits(fake, vpn_calls):
    start_to_ready(fake)
    fake.cutover_error = (409, "Worker is not usable: password=hunter2")

    with pytest.raises(db_workflow.NotAllowed) as exc:
        db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    assert "Worker is not usable" in str(exc.value) and "hunter2" not in str(exc.value)
    assert db_workflow.status(RUN_ID, agent=fake.client()).ready_for_cutover


# ---- finish


def to_stabilizing(fake):
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())
    wait_ended()


def test_finish_only_after_the_cutover(fake, vpn_calls):
    start_to_ready(fake)
    with pytest.raises(db_workflow.NotAllowed, match="use stop"):
        db_workflow.finish(RUN_ID, agent=fake.client())
    assert has_secret(f"secret://{RUN_ID}/source-db-admin")


def test_finish_without_a_job_is_refused(fake):
    with pytest.raises(db_workflow.NotAllowed, match="use stop"):
        db_workflow.finish(RUN_ID, agent=fake.client())


def test_finish_closes_then_removes_the_vpn_then_the_secrets(fake, vpn_calls, monkeypatch):
    order = []
    to_stabilizing(fake)
    put_secret(f"{RUN_ID}/vpn-context", {"json": "{}"})

    def destroy(run_id):
        order.append(("vpn", f"POST /jobs/{JOB_ID}/close" in fake.calls, has_secret(f"secret://{RUN_ID}/source-db-admin")))

    monkeypatch.setattr(vpn, "destroy_vpn", destroy)

    result = db_workflow.finish(RUN_ID, agent=fake.client())

    assert order == [("vpn", True, True)]
    assert result.state == "finished" and result.vpn == "removed" and result.remaining == []
    assert any("aws" in step for step in result.next_steps)
    assert result.target_accounts == {"retired": ["alpha_sink", "alpha_ddl"]}
    assert not any(has_secret(f"secret://{RUN_ID}/{tag}") for tag in (*INPUT_SECRETS, *db_workflow.OWN_SECRETS))
    assert db_workflow.status(RUN_ID, agent=fake.client()).cleanup_required is False


def test_finish_keeps_the_vpn_and_keys_when_close_fails_and_can_be_called_again(fake, vpn_calls):
    to_stabilizing(fake)
    fake.close_status = 502

    result = db_workflow.finish(RUN_ID, agent=fake.client())

    assert result.state == "incomplete" and "destroy_vpn" not in vpn_calls
    assert "agent_job" in result.remaining and "secret:source-db-admin" in result.remaining
    assert result.next_steps == []

    fake.close_status = None
    assert db_workflow.finish(RUN_ID, agent=fake.client()).state == "finished"


def test_finish_keeps_the_keys_when_the_vpn_stays_and_can_be_called_again(fake, vpn_calls, monkeypatch):
    to_stabilizing(fake)
    put_secret(f"{RUN_ID}/vpn-context", {"json": "{}"})

    def broken(run_id):
        raise RuntimeError(f"terraform failed: {SECRET_KEY}")

    monkeypatch.setattr(vpn, "destroy_vpn", broken)
    result = db_workflow.finish(RUN_ID, agent=fake.client())

    assert result.state == "incomplete" and result.vpn == "kept" and SECRET_KEY not in result.message
    assert {"vpn", "secret:source-db-admin"} <= set(result.remaining)
    assert db_workflow.status(RUN_ID, agent=fake.client()).cleanup_required

    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: True)
    second = db_workflow.finish(RUN_ID, agent=fake.client())

    assert second.state == "finished"
    assert fake.calls.count(f"POST /jobs/{JOB_ID}/close") == 1


def test_target_accounts_left_with_login_are_reported(fake, vpn_calls):
    fake.cleanup = {"target_accounts": {"error": "connection refused", "accounts": ["alpha_sink"]}}
    to_stabilizing(fake)

    result = db_workflow.finish(RUN_ID, agent=fake.client())

    # 에이전트의 비밀이 이미 지워져 다시 불러도 해결되지 않는다. 직접 할 일로 알린다.
    assert result.state == "finished" and result.remaining == []
    assert result.target_accounts["accounts"] == ["alpha_sink"]
    assert any("aws" in step for step in result.next_steps)
    assert any("alpha_sink" in step and "NOLOGIN" in step for step in result.next_steps)


def test_failure_after_the_switch_is_not_cleaned_up_automatically(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, "state": "failed", "phase": "cutover",
                                   "evidence": steps("switched_to_target", "failed")}
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    with pytest.raises(db_workflow.NotAllowed, match="by hand"):
        db_workflow.finish(RUN_ID, agent=fake.client())
    assert "destroy_vpn" not in vpn_calls


# ---- stop


def test_stop_before_the_cutover_cleans_up_everything(fake, vpn_calls):
    start_to_ready(fake)
    put_secret(f"{RUN_ID}/vpn-context", {"json": "{}"})

    result = db_workflow.stop(RUN_ID, agent=fake.client())

    assert result.state == "finished" and "destroy_vpn" in vpn_calls and result.remaining == []
    assert not has_secret(f"secret://{RUN_ID}/source-db-admin")


def test_stop_after_the_switch_is_refused(fake, vpn_calls):
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    with pytest.raises(db_workflow.NotAllowed, match="before cutover"):
        db_workflow.stop(RUN_ID, agent=fake.client())
    assert has_secret(f"secret://{RUN_ID}/source-db-admin") and "destroy_vpn" not in vpn_calls


def test_failed_stop_keeps_everything_and_can_be_called_again(fake, vpn_calls):
    start_to_ready(fake)
    fake.abort_status = 502

    result = db_workflow.stop(RUN_ID, agent=fake.client())

    assert result.state == "incomplete" and "agent_job" in result.remaining
    assert has_secret(f"secret://{RUN_ID}/source-db-admin") and "destroy_vpn" not in vpn_calls

    fake.abort_status = None
    assert db_workflow.stop(RUN_ID, agent=fake.client()).state == "finished"


def test_stop_during_the_vpn_stage_ends_the_run_without_a_job(fake, monkeypatch):
    building, release, calls = threading.Event(), threading.Event(), []

    def ensure(plan):
        building.set()
        release.wait(5)
        return {"tunnel_status": {}}

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", ensure)
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: calls.append("destroy_vpn"))
    db_workflow.run(START, agent=fake.client())
    building.wait(5)
    stopper = threading.Thread(target=lambda: calls.append(db_workflow.stop(RUN_ID, agent=fake.client()).state))
    stopper.start()
    wait_until(lambda: RUN_ID in db_workflow._stop_requested)
    release.set()
    stopper.join(5)

    assert calls == ["destroy_vpn", "finished"] and fake.job is None
    assert db_workflow.status(RUN_ID, agent=fake.client()).state == "stopped"


def test_target_secrets_under_another_name_are_kept_and_reported(fake, vpn_calls):
    put_secret("shared/target-admin", {"user": "postgres", "password": "shared-admin-pw"})
    put_secret(f"{RUN_ID}/other-refs", {"refs": json.dumps(["secret://shared/target-admin"])})
    run_id = start_to_ready(fake)
    db_workflow.cutover(run_id, "pm", agent=fake.client())
    wait_ended()

    result = db_workflow.finish(run_id, agent=fake.client())

    assert result.state == "finished" and has_secret("secret://shared/target-admin")
    assert any("secret://shared/target-admin" in step for step in result.next_steps)
    assert "shared-admin-pw" not in str(result.next_steps)


def test_finish_is_refused_after_a_mismatch_at_the_switch(fake, vpn_calls):
    def mismatch(job):
        return {**job, **SWITCHED, "state": "failed", "evidence": {
            **steps("switched_to_target", "released"), "verification": {"state": "mismatch"}}}

    fake.on_cutover = mismatch
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())

    with pytest.raises(db_workflow.NotAllowed, match="by hand"):
        db_workflow.finish(RUN_ID, agent=fake.client())
    assert "destroy_vpn" not in vpn_calls


def test_finish_during_the_comparison_can_be_called_again(fake, vpn_calls):
    to_stabilizing(fake)
    fake.close_status = 409

    waiting = db_workflow.finish(RUN_ID, agent=fake.client())

    assert waiting.state == "incomplete" and "destroy_vpn" not in vpn_calls
    fake.close_status = None
    assert db_workflow.finish(RUN_ID, agent=fake.client()).state == "finished"


def test_unfinished_comparison_is_told_as_a_next_step(fake, vpn_calls):
    to_stabilizing(fake)
    fake.job = {**fake.job, "evidence": {**fake.job["evidence"], "verification": {"state": "unverifiable"}}}

    result = db_workflow.finish(RUN_ID, agent=fake.client())

    assert result.state == "finished" and any("직접 확인" in step for step in result.next_steps)


def test_errors_hide_target_secrets_under_another_name(fake, monkeypatch):
    put_secret("shared/target-admin", {"user": "postgres", "password": "shared-admin-pw"})
    put_secret(f"{RUN_ID}/other-refs", {"refs": json.dumps(["secret://shared/target-admin"])})

    def broken(plan):
        raise db_workflow.VpnError("failed", "could not log in with shared-admin-pw")

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", broken)
    state = failed_status(fake)

    assert state.state == "failed" and "shared-admin-pw" not in state.message


def test_stop_of_a_job_that_failed_after_the_switch_is_refused(fake, vpn_calls):
    fake.on_cutover = lambda job: {**job, "state": "failed", "phase": "cutover",
                                   "evidence": steps("switched_to_target", "failed")}
    start_to_ready(fake)
    db_workflow.cutover(RUN_ID, "pm", agent=fake.client())
    wait_ended()

    with pytest.raises(db_workflow.NotAllowed, match="before cutover"):
        db_workflow.stop(RUN_ID, agent=fake.client())
    assert has_secret(f"secret://{RUN_ID}/source-db-admin") and "destroy_vpn" not in vpn_calls


def test_calls_during_a_cleanup_are_refused(fake, monkeypatch):
    removing, release = threading.Event(), threading.Event()

    def destroy(run_id):
        removing.set()
        release.wait(5)

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", lambda plan: {"tunnel_status": {}})
    monkeypatch.setattr(vpn, "destroy_vpn", destroy)
    to_stabilizing(fake)
    results = []
    first = threading.Thread(target=lambda: results.append(db_workflow.finish(RUN_ID, agent=fake.client()).state))
    first.start()
    removing.wait(5)

    for call in (db_workflow.finish, db_workflow.stop, db_workflow.stop_job, db_workflow.complete):
        with pytest.raises(db_workflow.NotAllowed, match="in progress"):
            call(RUN_ID, agent=fake.client())
    # 이 작업은 이미 끝나 run은 그 전에 거절된다. 에이전트를 읽지 못하는 경우에도 정리와 겹치지 않아야 한다.
    assert not db_workflow._reserve(RUN_ID)

    release.set()
    first.join(5)
    assert results == ["finished"] and not db_workflow._cleaning


def test_close_waits_as_long_as_abort():
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={})

    client = CdcAgentClient(AgentSettings(url="https://cdc.example", timeout_s=1, abort_timeout_s=77),
                            transport=httpx.MockTransport(handle))
    client.close(JOB_ID)
    client.abort(JOB_ID)

    assert seen == [77, 77]


def test_vpn_saved_without_a_db_port_can_still_be_removed():
    context = {"run_id": RUN_ID, "job_id": JOB_ID, "name": "n", "source": {"region": "r"}, "target": {}}
    put_secret(f"{RUN_ID}/vpn-context", {"json": json.dumps(context)})

    assert vpn.VpnContext.load(RUN_ID).source == {"region": "r", "db_port": 5432}


# ---- checks before anything is saved or built


@pytest.mark.parametrize("field", ["user", "password"])
@pytest.mark.parametrize("value", [" spaced", "spaced ", "two\nlines"])
def test_a_value_the_agent_would_refuse_is_refused_at_once(fake, vpn_calls, field, value):
    with pytest.raises(db_workflow.InvalidInput) as refused:
        db_workflow.run({**START, "source": {**START["source"], "db": {**START["source"]["db"], field: value}}},
                        agent=fake.client())

    assert refused.value.fields == [f"source.db.{field}"] and value not in str(refused.value)
    assert "ensure_vpn" not in vpn_calls


def test_a_saved_secret_the_agent_would_refuse_stops_before_the_vpn(fake, vpn_calls):
    put_secret(f"{RUN_ID}/target-sink-user", {"user": "alpha_sink", "password": " sink-pw"})

    state = failed_status(fake)

    assert state.state == "failed" and "target.password" in state.message and "sink-pw" not in state.message
    assert "ensure_vpn" not in vpn_calls


def test_a_run_that_ended_is_refused_before_its_keys_are_saved(fake, vpn_calls):
    start_to_ready(fake)
    db_workflow.stop(RUN_ID, agent=fake.client())
    wait_ended()
    assert not has_secret(f"secret://{RUN_ID}/source-db-admin")

    with pytest.raises(db_workflow.NotAllowed, match="new run_id"):
        db_workflow.run(START, agent=fake.client())
    assert not has_secret(f"secret://{RUN_ID}/source-db-admin")


def test_a_bad_setting_is_named_over_http_and_in_execute(http, monkeypatch):
    monkeypatch.setenv("CDC_LOG_INTERVAL_S", "soon")

    resp = http.post("/db-migration/runs", json=START)
    result = db_workflow.execute(INPUT)

    assert resp.status_code == 503 and "CDC_LOG_INTERVAL_S" in resp.json()["detail"]
    assert result.state == "failed" and "CDC_LOG_INTERVAL_S" in result.message


# ---- HTTP


@pytest.fixture
def http(fake):
    app.dependency_overrides[db_router.get_client] = fake.client
    yield TestClient(app)
    app.dependency_overrides.pop(db_router.get_client, None)


def test_http_flow(http, fake, vpn_calls):
    assert http.post("/db-migration/runs", json=START).json() == {"run_id": RUN_ID}
    wait_until(lambda: http.get(f"/db-migration/runs/{RUN_ID}").json()["ready_for_cutover"])

    cut = http.post(f"/db-migration/runs/{RUN_ID}/cutover", json={"approved_by": "pm"})
    wait_ended()
    done = http.post(f"/db-migration/runs/{RUN_ID}/finish")

    assert cut.json()["outcome"] == "switched" and done.json()["state"] == "finished"


def test_http_bad_input_quotes_no_value(http):
    resp = http.post("/db-migration/runs", json={**START, "source": {**START["source"], "region": SECRET_KEY}})

    assert resp.status_code == 422 and resp.json()["fields"] == ["source.region"]
    assert SECRET_KEY not in resp.text


def test_http_status_codes(http, fake, vpn_calls):
    assert http.get("/db-migration/runs/no-such-run").status_code == 404
    start_to_ready(fake)
    assert http.post(f"/db-migration/runs/{RUN_ID}/finish").status_code == 409


# ---- boundaries and settings


def test_module_does_not_import_other_features():
    folder = Path(db_workflow.__file__).parent
    for path in folder.glob("*.py"):
        text = path.read_text()
        for other in ("features.assessment", "features.provisioning", "features.workflows"):
            assert other not in text, path.name


def test_wiring_gives_assessment_the_references(monkeypatch):
    from app import wiring
    from app.features.assessment import service as assessment
    from app.features.assessment.schemas import AssessmentOutput
    from app.features.db_migration.schemas import SourceLookup

    seen = {}

    class Found:
        gaps = ["no ECS service"]

        class source:
            @staticmethod
            def model_dump():
                return {"db": {"host": "h"}}

    monkeypatch.setattr(assessment, "run", lambda inp: seen.update(inp.model_dump()) or Found)

    with pytest.raises(RuntimeError, match="no ECS service"):
        wiring.discover_source(SourceLookup(run_id="r", region="ap-northeast-2", target_region="asia-northeast3",
                                            cloud_ref="secret://r/source-cloud", db_admin_ref="secret://r/source-db-admin"))

    assert seen["aws_credentials_ref"] == "secret://r/source-cloud" and seen["db_admin_ref"] == "secret://r/source-db-admin"
    assert seen["target_region"] == "asia-northeast3"
    assert AssessmentOutput  # the shape wiring reads


def test_account_names_and_waits_come_from_settings(monkeypatch):
    monkeypatch.setenv("CDC_REPL_USER", "another_repl")
    monkeypatch.setenv("CDC_CUTOVER_WAIT_S", "5")
    monkeypatch.setenv("CDC_DB_SSLMODE", "verify-full")

    assert tunables().repl_user == "another_repl" and tunables().cutover_wait_s == 5
    assert db_workflow.ensure_repl_account(RUN_ID)["user"] == "another_repl"
    request = db_workflow.build_job_request(JOB_ID, INPUT["source"] | {"db": {"host": "h", "port": 1, "database": "d"}},
                                            INPUT["target"] | {"db": {"private_ip": "i", "port": 1, "database": "d"}})
    assert request["source"]["sslmode"] == request["target"]["sslmode"] == "verify-full"


def test_a_setting_that_is_not_a_number_is_named(monkeypatch):
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "soon")
    with pytest.raises(MissingSetting, match="CDC_PROBE_WAIT_S"):
        tunables()


def test_no_environment_value_is_written_in_the_module():
    folder = Path(db_workflow.__file__).parent
    for path in folder.glob("*.py"):
        text = path.read_text()
        for value in ("ap-singapore", "ap-northeast", "10.50.", "asia-northeast", "ocid1."):
            assert value not in text, f"{value} in {path.name}"
