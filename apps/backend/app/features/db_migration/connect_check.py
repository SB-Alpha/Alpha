"""8번: VPN 뒤 CDC 노드에서 양쪽 DB의 host·port에 닿는지 확인한다.

DB 로그인과 테이블 조회는 하지 않는다. 그것은 9번의 일이다.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

from .execution_model import ExecutionModel


class ConnectivityError(RuntimeError):
    """닿지 않는 host:port만 담는다."""


class ProbeAgent(Protocol):
    def probe(self, endpoints: list[dict]) -> list[dict]: ...


def endpoints_of(execution: ExecutionModel) -> list[dict]:
    return [{"host": side.db.host, "port": side.db.port} for side in (execution.source, execution.target)]


def wait_reachable(
    agent: ProbeAgent,
    endpoints: list[dict],
    *,
    wait_s: float,
    retry_s: float,
    stop_requested: Callable[[], bool] = lambda: False,
    on_result: Callable[[Any], None] = lambda result: None,
) -> None:
    """터널이 올라온 뒤 경로가 퍼지는 데 시간이 걸리므로 wait_s까지 다시 본다. stop이 오면 판정 없이 돌아간다."""
    deadline = time.monotonic() + wait_s
    while not stop_requested():
        result = agent.probe(endpoints)
        on_result(result)
        unreachable = [f"{r['host']}:{r['port']}" for r in result if not r["ok"]]
        if not unreachable:
            return
        if time.monotonic() > deadline:
            raise ConnectivityError(f"not reachable from the CDC nodes: {', '.join(unreachable)}")
        time.sleep(retry_s)
