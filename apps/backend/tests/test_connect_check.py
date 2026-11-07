"""8: the reachability wait, and the start checks reading the common model."""

import pytest

from app.features.db_migration.connect_check import (
    ConnectivityError,
    endpoints_of,
    wait_reachable,
)
from app.features.db_migration.start_checks import (
    CdcNetwork,
    StartCheckError,
    StartCheckInput,
    check_networks,
)

from .test_job_request import catalog, table

ENDPOINTS = [{"host": "10.1.0.5", "port": 5432}, {"host": "10.2.0.5", "port": 5432}]


class ProbeAgent:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def probe(self, endpoints):
        self.calls += 1
        ok = self.answers.pop(0) if self.answers else False
        return [{**endpoint, "ok": ok, "error": None if ok else "timeout"} for endpoint in endpoints]


def test_endpoints_come_from_the_model_on_any_route():
    assert endpoints_of(catalog(table()).execution) == ENDPOINTS


def test_a_later_answer_passes_while_routes_spread():
    agent = ProbeAgent(False, False, True)

    wait_reachable(agent, ENDPOINTS, wait_s=5, retry_s=0)

    assert agent.calls == 3


def test_past_the_wait_names_what_is_unreachable():
    with pytest.raises(ConnectivityError, match=r"10\.1\.0\.5:5432, 10\.2\.0\.5:5432"):
        wait_reachable(ProbeAgent(), ENDPOINTS, wait_s=0, retry_s=0)


def test_stop_ends_the_wait_without_a_verdict():
    agent = ProbeAgent(False, False)
    stops = iter([False, True])

    wait_reachable(agent, ENDPOINTS, wait_s=5, retry_s=0, stop_requested=lambda: next(stops))

    assert agent.calls == 1


def test_results_are_handed_to_the_caller():
    seen = []

    wait_reachable(ProbeAgent(True), ENDPOINTS, wait_s=0, retry_s=0, on_result=seen.append)

    assert seen[0][0]["ok"] is True


def test_start_checks_read_the_common_model():
    inp = StartCheckInput.from_route(catalog(table()).execution)

    assert (inp.source_host, inp.source_port, inp.source_cidrs) == ("10.1.0.5", 5432, ("10.0.0.0/16",))
    assert inp.source_publicly_accessible is False
    with pytest.raises(StartCheckError, match="overlap"):
        check_networks(inp, CdcNetwork(vcn_cidr="10.50.0.0/16", node_subnet_cidr="10.50.1.0/24"))
