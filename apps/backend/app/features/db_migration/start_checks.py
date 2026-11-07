"""004의 6번: VPN 구축 전에 조회 결과의 공통 값만 검사한다.

사용법: check_start(StartCheckInput.from_route(found), job_id=job_id,
                 agent=agent, network=CdcNetwork.from_env())

클라우드 API, VPN 구축, DB 접속·테이블 조회, 비밀 저장은 하지 않는다.
새 입력의 source.db 계정은 앱이 쓰는 계정이다. 별도 앱 계정은 받지 않는다.
동시 실행 검사는 에이전트의 현재 기록만 확인한다. 작업 예약/잠금은 호출자가 맡는다.
"""

import ipaddress
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)
RELEASED_STATES = frozenset({"aborted", "succeeded"})
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class StartCheckError(RuntimeError):
    """원문 응답이나 비밀값을 인용하지 않는 검사 실패. code는 호출자의 분기용이다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AgentReader(Protocol):
    def status(self) -> dict: ...

    def list_jobs(self) -> list[dict]: ...


class DatabaseFacts(Protocol):
    @property
    def host(self) -> str: ...

    @property
    def port(self) -> int: ...

    @property
    def publicly_accessible(self) -> bool | None: ...


class EndpointFacts(Protocol):
    @property
    def db(self) -> DatabaseFacts: ...

    @property
    def cidrs(self) -> tuple[str, ...]: ...


class RouteFacts(Protocol):
    @property
    def source(self) -> EndpointFacts: ...

    @property
    def target(self) -> EndpointFacts: ...


@dataclass(frozen=True)
class CdcNetwork:
    vcn_cidr: str
    node_subnet_cidr: str

    @classmethod
    def from_env(cls) -> "CdcNetwork":
        # 006의 기존 변수 두 개만 읽는다. VPN의 인증/구축 설정은 여기서 필요 없다.
        names = ("CDC_OCI_VCN_CIDR", "CDC_NODE_SUBNET_CIDR")
        values = [os.getenv(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise StartCheckError("missing_setting", f"environment variable required: {', '.join(missing)}")
        return cls(*values)


@dataclass(frozen=True)
class StartCheckInput:
    source_host: str
    source_port: int
    source_publicly_accessible: bool | None
    source_cidrs: tuple[str, ...]
    target_cidrs: tuple[str, ...]
    # 9번의 DB 이름 조회 전에는 None. 같은 host/port의 모든 DB를 경쟁 대상으로 본다.
    source_database: str | None = None

    @classmethod
    def from_route(cls, route: RouteFacts, *, source_database: str | None = None) -> "StartCheckInput":
        return cls(
            source_host=route.source.db.host,
            source_port=route.source.db.port,
            source_publicly_accessible=route.source.db.publicly_accessible,
            source_cidrs=tuple(route.source.cidrs),
            target_cidrs=tuple(route.target.cidrs),
            source_database=source_database,
        )


def check_start(inp: StartCheckInput, *, job_id: str, agent: AgentReader, network: CdcNetwork) -> None:
    """실패는 예외, 통과는 None. 성공/실패를 진행 알림으로 대신하지 않는다 (005)."""
    _check_source(inp)
    check_networks(inp, network)
    check_agent(agent)
    check_not_running(inp, job_id=job_id, agent=agent)


def _host(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(c.isspace() for c in value):
        raise StartCheckError("invalid_source", "source host must be a non-empty address")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        normalized = value.lower().rstrip(".")
        if not normalized:
            raise StartCheckError("invalid_source", "source host must be a non-empty address")
        return normalized


def _check_source(inp: StartCheckInput) -> None:
    _host(inp.source_host)
    if type(inp.source_port) is not int or not 1 <= inp.source_port <= 65535:
        raise StartCheckError("invalid_source", "source port must be between 1 and 65535")
    if inp.source_database is not None and (
        not inp.source_database or inp.source_database != inp.source_database.strip()
        or "\n" in inp.source_database or "\r" in inp.source_database
    ):
        raise StartCheckError("invalid_source", "source database must be a non-empty single line")
    if inp.source_publicly_accessible is not None and type(inp.source_publicly_accessible) is not bool:
        raise StartCheckError("invalid_source", "source public access must be true, false or unknown")
    if inp.source_publicly_accessible is True:
        raise StartCheckError("source_public_access", "the source DB has public access on; turn it off first")
    if inp.source_publicly_accessible is None:
        logger.warning("public access of the source DB is unknown")


def _ranges(field: str, values: tuple[str, ...]) -> list[Network]:
    if not values:
        raise StartCheckError("missing_cidr", f"{field} must contain at least one CIDR block")
    result = []
    for value in values:
        try:
            result.append(ipaddress.ip_network(value, strict=False))
        except (ValueError, TypeError):
            raise StartCheckError("invalid_cidr", f"{field} is not a CIDR block") from None
    return result


def check_networks(inp: StartCheckInput, network: CdcNetwork) -> None:
    source = _ranges("source.cidrs", inp.source_cidrs)
    target = _ranges("target.cidrs", inp.target_cidrs)
    vcn = _ranges("CDC_OCI_VCN_CIDR", (network.vcn_cidr,))
    node = _ranges("CDC_NODE_SUBNET_CIDR", (network.node_subnet_cidr,))[0]
    if node.version != vcn[0].version or not node.subnet_of(vcn[0]):
        raise StartCheckError("node_outside_vcn", "CDC_NODE_SUBNET_CIDR is not inside CDC_OCI_VCN_CIDR")
    # 한쪽 내부의 VPC와 서브넷 포함 관계는 정상이다. 서로 다른 쪽끼리만 비교한다.
    for left_name, left, right_name, right in (
        ("source.cidrs", source, "target.cidrs", target),
        ("source.cidrs", source, "CDC_OCI_VCN_CIDR", vcn),
        ("target.cidrs", target, "CDC_OCI_VCN_CIDR", vcn),
    ):
        if any(a.version == b.version and a.overlaps(b) for a in left for b in right):
            raise StartCheckError("cidr_overlap", f"address ranges overlap: {left_name}, {right_name}")


def check_agent(agent: AgentReader) -> None:
    try:
        status = agent.status()
    except Exception:
        raise StartCheckError("agent_unavailable", "cannot read CDC cluster status") from None
    if not isinstance(status, dict):
        raise StartCheckError("invalid_agent_response", "invalid CDC cluster status")
    problems = [name for name in ("kafka", "connect") if status.get(name) is not True]
    missing = status.get("plugins_missing")
    # Connect가 죽으면 실제 에이전트는 plugins_missing=None을 보낸다.
    if not isinstance(missing, list) and not (missing is None and status.get("connect") is False):
        raise StartCheckError("invalid_agent_response", "invalid CDC plugin status")
    if missing:
        problems.append("connector plugins")
    if problems:
        raise StartCheckError("agent_not_ready", f"CDC cluster not ready: {', '.join(problems)}")


def check_not_running(inp: StartCheckInput, *, job_id: str, agent: AgentReader) -> None:
    try:
        jobs = agent.list_jobs()
    except Exception:
        raise StartCheckError("agent_unavailable", "cannot read CDC jobs") from None
    if not isinstance(jobs, list):
        raise StartCheckError("invalid_agent_response", "invalid CDC job list")
    for job in jobs:
        try:
            if not isinstance(job, Mapping) or not isinstance(job["job_id"], str) or not isinstance(job["state"], str):
                raise ValueError
            if job["job_id"] == job_id or job["state"] in RELEASED_STATES:
                continue
            source = job["request"]["source"]
            port = source.get("port", 5432)
            database = source["database"]
            if type(port) is not int or not 1 <= port <= 65535 or not isinstance(database, str) or not database:
                raise ValueError
            same_server = (_host(source["host"]), port) == (_host(inp.source_host), inp.source_port)
        except (KeyError, TypeError, ValueError, AttributeError, StartCheckError):
            raise StartCheckError("invalid_agent_response", "invalid CDC job source") from None
        if same_server and (inp.source_database is None or inp.source_database == database):
            # failed는 해제 완료가 아니다. 복제 슬롯이나 쓰기 차단이 남아 있을 수 있다.
            raise StartCheckError("source_busy", "another job already works on the same source DB; finish or stop it first")
