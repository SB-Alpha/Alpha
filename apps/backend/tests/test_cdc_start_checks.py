"""시작 전 검사의 6개 경로, 실패 경계, VPN 전 호출 계약을 검증한다."""

from dataclasses import replace
from itertools import permutations
from types import SimpleNamespace

import pytest

from app.features.db_migration.start_checks import (
    CdcNetwork,
    StartCheckError,
    StartCheckInput,
    check_start,
)

NETWORK = CdcNetwork("10.50.0.0/16", "10.50.1.0/24")
INPUT = StartCheckInput("source.example", 5432, False, ("172.31.0.0/16",), ("10.20.0.0/16",))
SECRET = "secret-response-marker"


class Agent:
    def __init__(self, *, health=None, jobs=None, error_at=None):
        self.health = health if health is not None else {"kafka": True, "connect": True, "plugins_missing": []}
        self.jobs = jobs if jobs is not None else []
        self.error_at = error_at
        self.calls = []

    def status(self):
        self.calls.append("status")
        if self.error_at == "status":
            raise RuntimeError(SECRET)
        return self.health

    def list_jobs(self):
        self.calls.append("jobs")
        if self.error_at == "jobs":
            raise RuntimeError(SECRET)
        return self.jobs


def job(*, state="running", job_id="other", **source):
    return {
        "job_id": job_id,
        "state": state,
        "request": {"source": {"host": "source.example", "database": "app", **source}},
    }


def run(inp=INPUT, *, agent=None, network=NETWORK):
    return check_start(inp, job_id="current", agent=agent or Agent(), network=network)


def endpoint(cloud, *, source):
    return SimpleNamespace(
        cloud=cloud, region="region", scope_id="scope",
        db=SimpleNamespace(host="source.example" if source else "target.example", port=5432, publicly_accessible=False),
        cidrs=("172.31.0.0/16",) if source else ("10.20.0.0/16",),
        # 클라우드별 네트워크 값이 비어 있어도 공통 검사에는 영향이 없다.
        network={},
    )


@pytest.mark.parametrize("source,target", tuple(permutations(("aws", "gcp", "azure"), 2)))
def test_all_six_routes_use_common_fields(source, target):
    found = SimpleNamespace(source=endpoint(source, source=True), target=endpoint(target, source=False))
    inp = StartCheckInput.from_route(found)
    agent = Agent()
    assert run(inp, agent=agent) is None
    assert agent.calls == ["status", "jobs"]


@pytest.mark.parametrize("inp,network,code", [
    (replace(INPUT, source_publicly_accessible=True), NETWORK, "source_public_access"),
    (replace(INPUT, source_host=""), NETWORK, "invalid_source"),
    (replace(INPUT, source_host="..."), NETWORK, "invalid_source"),
    (replace(INPUT, source_port=0), NETWORK, "invalid_source"),
    (replace(INPUT, source_port=True), NETWORK, "invalid_source"),
    (replace(INPUT, source_port=65536), NETWORK, "invalid_source"),
    (replace(INPUT, source_database=""), NETWORK, "invalid_source"),
    (replace(INPUT, source_publicly_accessible="false"), NETWORK, "invalid_source"),
    (replace(INPUT, source_cidrs=()), NETWORK, "missing_cidr"),
    (replace(INPUT, target_cidrs=()), NETWORK, "missing_cidr"),
    (replace(INPUT, source_cidrs=(SECRET,)), NETWORK, "invalid_cidr"),
    (replace(INPUT, target_cidrs=(SECRET,)), NETWORK, "invalid_cidr"),
    (INPUT, CdcNetwork(SECRET, NETWORK.node_subnet_cidr), "invalid_cidr"),
    (INPUT, CdcNetwork(NETWORK.vcn_cidr, SECRET), "invalid_cidr"),
    (INPUT, CdcNetwork(NETWORK.vcn_cidr, "10.51.1.0/24"), "node_outside_vcn"),
    (INPUT, CdcNetwork(NETWORK.vcn_cidr, "fd00::/64"), "node_outside_vcn"),
    (replace(INPUT, source_cidrs=("10.20.1.0/24",)), NETWORK, "cidr_overlap"),
    (replace(INPUT, source_cidrs=("10.50.1.0/24",)), NETWORK, "cidr_overlap"),
    (replace(INPUT, target_cidrs=("10.50.2.0/24",)), NETWORK, "cidr_overlap"),
    # 기본 대역뿐 아니라 추가 대역도 검사한다.
    (replace(INPUT, source_cidrs=(*INPUT.source_cidrs, "10.20.3.0/24")), NETWORK, "cidr_overlap"),
    (replace(INPUT, target_cidrs=(*INPUT.target_cidrs, "172.31.1.0/24")), NETWORK, "cidr_overlap"),
])
def test_invalid_inputs_stop_before_contacting_agent(inp, network, code):
    agent = Agent()
    with pytest.raises(StartCheckError) as exc:
        run(inp, agent=agent, network=network)
    assert exc.value.code == code
    assert SECRET not in str(exc.value)
    assert not agent.calls


def test_unknown_public_access_keeps_existing_warning_policy(caplog):
    run(replace(INPUT, source_publicly_accessible=None))
    assert "public access of the source DB is unknown" in caplog.text


def test_contained_ranges_within_one_side_are_normal():
    run(replace(INPUT, target_cidrs=("10.20.0.0/16", "10.20.1.0/24")))


def test_ipv4_and_ipv6_do_not_overlap():
    run(replace(INPUT, target_cidrs=("fd01::/64",)))


def test_ipv6_overlap_and_node_containment():
    inp = replace(INPUT, source_cidrs=("fd01::/64",), target_cidrs=("fd02::/64",))
    network = CdcNetwork("fd03::/48", "fd03:0:0:1::/64")
    run(inp, network=network)
    with pytest.raises(StartCheckError, match="overlap"):
        run(replace(inp, target_cidrs=("fd01::/80",)), network=network)


@pytest.mark.parametrize("health,code", [
    ({"kafka": False, "connect": True, "plugins_missing": []}, "agent_not_ready"),
    ({"kafka": True, "connect": False, "plugins_missing": []}, "agent_not_ready"),
    ({"kafka": True, "connect": False, "plugins_missing": None}, "agent_not_ready"),
    ({"kafka": True, "connect": True, "plugins_missing": [SECRET]}, "agent_not_ready"),
    ({"kafka": True, "connect": True}, "invalid_agent_response"),
    ([], "invalid_agent_response"),
])
def test_unhealthy_agent_stops_before_reading_jobs(health, code):
    agent = Agent(health=health)
    with pytest.raises(StartCheckError) as exc:
        run(agent=agent)
    assert exc.value.code == code
    assert SECRET not in str(exc.value)
    assert agent.calls == ["status"]


@pytest.mark.parametrize("error_at", ["status", "jobs"])
def test_agent_errors_do_not_quote_remote_response(error_at):
    with pytest.raises(StartCheckError) as exc:
        run(agent=Agent(error_at=error_at))
    assert exc.value.code == "agent_unavailable"
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("state", ["running", "waiting_approval", "failed", "unknown"])
def test_unfinished_job_blocks_source_even_before_database_discovery(state):
    with pytest.raises(StartCheckError) as exc:
        run(agent=Agent(jobs=[job(state=state)]))
    assert exc.value.code == "source_busy"


@pytest.mark.parametrize("state", ["succeeded", "aborted"])
def test_released_job_does_not_block_source(state):
    run(agent=Agent(jobs=[job(state=state)]))


def test_same_job_is_idempotent():
    run(agent=Agent(jobs=[job(job_id="current")]))


def test_different_host_or_port_is_another_source():
    run(agent=Agent(jobs=[job(host="another.example"), job(port=6432)]))


def test_known_database_limits_conflict_to_that_database():
    inp = replace(INPUT, source_database="orders")
    run(inp, agent=Agent(jobs=[job(database="app")]))
    with pytest.raises(StartCheckError, match="same source DB"):
        run(inp, agent=Agent(jobs=[job(database="orders")]))


def test_dns_host_case_trailing_dot_and_default_port_do_not_hide_conflict():
    with pytest.raises(StartCheckError, match="same source DB"):
        run(agent=Agent(jobs=[job(host="SOURCE.EXAMPLE.")]))


def test_ipv6_spelling_does_not_hide_conflict():
    with pytest.raises(StartCheckError, match="same source DB"):
        run(replace(INPUT, source_host="fd01::1"), agent=Agent(jobs=[job(host="fd01:0:0:0:0:0:0:1")]))


@pytest.mark.parametrize("jobs", [
    {}, [None], [{}], [{"job_id": "other", "state": "running"}],
    [job(port=SECRET)], [job(host=SECRET, database=None)],
])
def test_unreadable_jobs_cannot_be_treated_as_no_conflicts(jobs):
    with pytest.raises(StartCheckError) as exc:
        run(agent=Agent(jobs=jobs))
    assert exc.value.code == "invalid_agent_response"
    assert SECRET not in str(exc.value)


def test_network_settings_use_only_two_of_the_existing_twelve_variables(monkeypatch):
    monkeypatch.setenv("CDC_OCI_VCN_CIDR", NETWORK.vcn_cidr)
    monkeypatch.setenv("CDC_NODE_SUBNET_CIDR", NETWORK.node_subnet_cidr)
    monkeypatch.delenv("CDC_OCI_DRG_ID", raising=False)
    assert CdcNetwork.from_env() == NETWORK
    monkeypatch.delenv("CDC_NODE_SUBNET_CIDR")
    with pytest.raises(StartCheckError, match="CDC_NODE_SUBNET_CIDR") as exc:
        CdcNetwork.from_env()
    assert exc.value.code == "missing_setting"
