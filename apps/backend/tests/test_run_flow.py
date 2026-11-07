"""The new run from its input to the cutover wait on all six routes. Clouds, VPN and agent are fakes."""

import json
import logging
import threading
from itertools import permutations

import httpx
import pytest

from app.core.secrets import has_secret, put_secret, resolve
from app.features.db_migration import cloud_lookup, db_workflow, vpn, vpn_workflow

from .test_db_workflow import (
    FakeAgent,
    env,  # noqa: F401 - the same store and settings
    found_endpoint,
    wait_until,
)
from .test_run_input import CREDENTIALS

RUN_ID = "mig-flow"
JOB_ID = "mig_flow"
REGIONS = {"aws": "ap-northeast-2", "gcp": "asia-northeast3", "azure": "koreacentral"}
SOURCE_PASSWORD = "source-password-marker"
TARGET_PASSWORD = "target-password-marker"
SIX_ROUTES = tuple(permutations(REGIONS, 2))
TABLE = {"schema_name": "public", "name": "items", "primary_key": ["id"], "kind": "table",
         "is_partition": False, "can_select": True}


class RecordingAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.request = None
        self.secrets = None
        self.catalog_queries = []
        self.databases = {"10.1.0.5": ["shop"], "10.2.0.5": ["shop_new"]}
        self.tables = [TABLE]
        self.unreachable = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/catalog":
            self.calls.append("POST /catalog")
            query = json.loads(request.content)
            self.catalog_queries.append(query)
            if query["database"] is None:
                return httpx.Response(200, json={"database": None, "tables": [],
                                                 "databases": self.databases[query["host"]]})
            return httpx.Response(200, json={"database": query["database"], "databases": [], "tables": self.tables})
        if path == "/probe" and self.unreachable:
            self.calls.append("POST /probe")
            return httpx.Response(200, json=[{**e, "ok": False, "error": "timeout"} for e in json.loads(request.content)])
        if path == "/jobs" and request.method == "POST":
            self.request = json.loads(request.content)
        if path.endswith("/secrets"):
            self.secrets = json.loads(request.content)
        return super().handle(request)


def run_input(source="aws", target="gcp", source_db=None):
    return {
        "run_id": RUN_ID,
        "source": {"cloud": source, "region": REGIONS[source],
                   "db": {"user": "app_owner", "password": SOURCE_PASSWORD, **(source_db or {})}},
        "target": {"cloud": target, "region": REGIONS[target],
                   "db": {"user": "admin", "password": TARGET_PASSWORD, "app_user": "app_new"}},
    }


@pytest.fixture
def agent():
    return RecordingAgent()


@pytest.fixture
def fake(agent):
    """The store fixture cleans up through this agent."""
    return agent


@pytest.fixture
def vpn_plans(monkeypatch):
    plans = []
    monkeypatch.setattr(vpn_workflow, "ensure_vpn", lambda plan: plans.append(plan) or {"tunnel_status": {}})
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: True)
    return plans


@pytest.fixture(autouse=True)
def clouds(monkeypatch, env):  # noqa: F811
    def find_route(injected):
        return cloud_lookup.FoundRoute(
            source=found_endpoint(injected.source.cloud, injected.source.region, "10.1.0.5", ("10.1.0.0/16",)),
            target=found_endpoint(injected.target.cloud, injected.target.region, "10.2.0.5", ("10.2.0.0/16",)),
        )

    monkeypatch.setattr(cloud_lookup, "find_route", find_route)
    for cloud, value in CREDENTIALS.items():
        put_secret(f"cloud/{cloud}", value)


def start(agent, inp=None):
    return db_workflow.run(inp or run_input(), agent=agent.client())


def ended(agent):
    wait_until(lambda: RUN_ID not in db_workflow._active)
    return db_workflow.status(RUN_ID, agent=agent.client())


def to_ready(agent, inp=None):
    start(agent, inp)
    wait_until(lambda: db_workflow.status(RUN_ID, agent=agent.client()).ready_for_cutover)


@pytest.mark.parametrize(("source", "target"), SIX_ROUTES)
def test_every_route_reaches_the_cutover_wait_with_the_same_steps(agent, vpn_plans, source, target):
    to_ready(agent, run_input(source, target))

    assert (vpn_plans[0].source.cloud, vpn_plans[0].target.cloud) == (source, target)
    assert agent.request["source"]["host"] == "10.1.0.5" and agent.request["target"]["host"] == "10.2.0.5"
    assert agent.request["source"]["database"] == "shop" and agent.request["target"]["database"] == "shop_new"
    status = db_workflow.status(RUN_ID, agent=agent.client())
    assert status.route.source.cloud == source and status.route.target.cloud == target
    assert status.metrics["target_db_network"] == "PRIVATE"


def test_steps_run_in_the_order_the_catalog_needs(agent, monkeypatch):
    def ensure(plan):
        agent.calls.append("vpn")
        return {"tunnel_status": {}}

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", ensure)
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: True)

    to_ready(agent)

    positions = [agent.calls.index(call) for call in
                 ("GET /status", "GET /jobs", "vpn", "POST /probe", "POST /catalog",
                  f"PUT /jobs/{JOB_ID}/secrets", "POST /jobs")]
    assert positions == sorted(positions)


def test_job_request_uses_the_catalog_and_carries_no_secret(agent, vpn_plans):
    to_ready(agent)

    assert agent.request["source"]["tables"] == ["public.items"]
    assert agent.request["source"]["app_role"] == "app_owner" and agent.request["target"]["app_role"] == "app_new"
    assert agent.request["target"]["keep_ddl_user"] is False
    assert SOURCE_PASSWORD not in json.dumps(agent.request) and TARGET_PASSWORD not in json.dumps(agent.request)


def test_agent_gets_the_input_accounts_and_the_accounts_this_run_made(agent, vpn_plans):
    to_ready(agent)

    assert agent.secrets["source.app.user"] == agent.secrets["source.admin.user"] == "app_owner"
    assert agent.secrets["source.admin.password"] == SOURCE_PASSWORD
    assert agent.secrets["target.admin.password"] == TARGET_PASSWORD
    assert agent.secrets["source.password"] == resolve(f"secret://{RUN_ID}/source-repl")["password"]
    assert agent.secrets["target.ddl.password"] == resolve(f"secret://{RUN_ID}/target-ddl-user")["password"]
    assert agent.secrets["target.password"] == resolve(f"secret://{RUN_ID}/target-sink-user")["password"]


def test_status_and_logs_carry_no_secret(agent, vpn_plans, caplog):
    with caplog.at_level(logging.INFO):
        to_ready(agent)

    shown = db_workflow.status(RUN_ID, agent=agent.client()).model_dump_json() + caplog.text
    for secret in (SOURCE_PASSWORD, TARGET_PASSWORD, CREDENTIALS["aws"]["aws_secret_access_key"]):
        assert secret not in shown


def test_cutover_and_finish_work_on_the_new_run(agent, vpn_plans):
    to_ready(agent)

    assert db_workflow.cutover(RUN_ID, "pm", agent=agent.client()).outcome == "switched"
    wait_until(lambda: RUN_ID not in db_workflow._active)
    assert db_workflow.finish(RUN_ID, agent=agent.client()).state == "finished"
    assert not has_secret(f"secret://{RUN_ID}/source-db-admin") and not has_secret(f"secret://{RUN_ID}/source-repl")
    assert has_secret("secret://cloud/aws")


def test_many_databases_name_the_choices_then_a_retry_with_the_choice_goes_on(agent, vpn_plans):
    agent.databases["10.1.0.5"] = ["shop", "billing"]

    status = (start(agent), ended(agent))[1]

    assert status.state == "failed" and "shop" in status.message and "billing" in status.message
    assert "POST /jobs" not in agent.calls

    to_ready(agent, run_input(source_db={"database": "billing"}))

    assert agent.request["source"]["database"] == "billing"
    assert len(vpn_plans) == 2


def test_a_chosen_database_that_is_not_there_is_refused(agent, vpn_plans):
    status = (start(agent, run_input(source_db={"database": "nope"})), ended(agent))[1]

    assert status.state == "failed" and "selected database" in status.message
    assert "POST /jobs" not in agent.calls


def test_table_without_a_primary_key_stops_before_the_job_and_keeps_the_vpn(agent, vpn_plans):
    agent.tables = [TABLE, {**TABLE, "name": "audit", "primary_key": []}]

    status = (start(agent), ended(agent))[1]

    assert status.state == "failed" and "public.audit" in status.message
    assert "POST /jobs" not in agent.calls and agent.secrets is None
    assert status.cleanup_required


def test_unreachable_database_stops_before_the_catalog(agent, vpn_plans, monkeypatch):
    monkeypatch.setenv("CDC_PROBE_WAIT_S", "0")
    agent.unreachable = True

    status = (start(agent), ended(agent))[1]

    assert status.state == "failed" and status.phase == "probe" and "10.1.0.5:5432" in status.message
    assert "POST /catalog" not in agent.calls


def test_stop_during_the_vpn_removes_it_and_registers_no_job(agent, monkeypatch):
    building, release, removed = threading.Event(), threading.Event(), []

    def ensure(plan):
        building.set()
        release.wait(5)
        return {"tunnel_status": {}}

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", ensure)
    monkeypatch.setattr(vpn, "destroy_vpn", lambda run_id: removed.append(run_id) or True)
    start(agent)
    assert building.wait(5)

    results = []
    stopper = threading.Thread(target=lambda: results.append(db_workflow.stop(RUN_ID, agent=agent.client())))
    stopper.start()
    wait_until(lambda: RUN_ID in db_workflow._stop_requested)
    release.set()
    stopper.join(5)

    assert results[0].state == "finished" and removed == [RUN_ID]
    assert agent.request is None and "POST /catalog" not in agent.calls


def test_an_error_quoting_a_cloud_key_hides_it(agent, monkeypatch):
    secret = CREDENTIALS["aws"]["aws_secret_access_key"]

    def broken(plan):
        raise db_workflow.VpnError("failed", f"terraform said {secret}")

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", broken)

    status = (start(agent), ended(agent))[1]

    assert status.state == "failed" and secret not in status.message and "terraform said" in status.message


def test_an_unexpected_error_shows_no_original_text(agent, monkeypatch):
    def broken(plan):
        raise RuntimeError(f"unexpected {SOURCE_PASSWORD} detail-marker")

    monkeypatch.setattr(vpn_workflow, "ensure_vpn", broken)

    status = (start(agent), ended(agent))[1]

    assert status.state == "failed" and "detail-marker" not in status.message
    assert "다시 실행" in status.message


def test_another_run_on_the_same_source_server_is_refused(agent, vpn_plans, monkeypatch):
    monkeypatch.setitem(db_workflow._runs, "other", db_workflow._Progress(stage="vpn", source=("10.1.0.5", "5432")))
    db_workflow._active.add("other")
    try:
        status = (start(agent), ended(agent))[1]
    finally:
        db_workflow._active.discard("other")

    assert status.state == "failed" and "same source DB" in status.message and vpn_plans == []
