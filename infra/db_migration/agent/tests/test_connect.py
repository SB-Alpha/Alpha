import httpx
import pytest

from cdc_agent import connect
from cdc_agent.connect import ConnectClient


def client(responses: list[int], calls: list[str]) -> ConnectClient:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(responses.pop(0) if responses else 200, json={})

    c = ConnectClient("http://connect")
    c._http = httpx.Client(base_url="http://connect", transport=httpx.MockTransport(handle))
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connect.time, "sleep", lambda s: None)


def test_register_waits_out_a_rebalance():
    calls: list[str] = []
    client([409, 409, 200], calls).put_config("c", {})
    assert calls == ["PUT"] * 3


def test_delete_waits_out_a_rebalance_and_accepts_not_found():
    calls: list[str] = []
    client([409, 404], calls).delete("c")
    assert calls == ["DELETE"] * 2


def test_register_gives_up_when_the_conflict_stays(monkeypatch):
    monkeypatch.setattr(connect, "REBALANCE_WAIT_S", -1)
    with pytest.raises(httpx.HTTPStatusError):
        client([409], []).put_config("c", {})


def test_pause_does_not_wait_because_writes_are_on_hold():
    calls: list[str] = []
    with pytest.raises(httpx.HTTPStatusError):
        client([409], calls).pause("c")
    assert calls == ["PUT"]


def test_connector_level_failure_is_reported():
    status = {"connector": {"state": "FAILED", "trace": "bad config\nat line"}, "tasks": []}
    assert connect.connector_error(status) == "bad config"


def test_restart_failed_asks_for_failed_tasks_only_and_allows_a_missing_connector():
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(404)

    client = ConnectClient("http://connect")
    client._http = httpx.Client(base_url="http://connect", transport=httpx.MockTransport(handle))
    client.restart_failed("c1")
    assert seen == ["http://connect/connectors/c1/restart?includeTasks=true&onlyFailed=true"]
