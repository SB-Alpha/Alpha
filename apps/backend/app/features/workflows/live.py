"""대시보드 3D 토폴로지·전환 화면이 보는 실시간 엣지 상태 (읽기 전용).

Cloudflare Worker 게이트 상태, CDC 에이전트 상태, 외부 프로버 1회 측정을 한 번에 돌려줍니다.
모르는 값은 None 으로 둡니다. 설정이 없으면 "not_configured", 호출이 실패하면 "error" 이며 값은 모두 None 입니다.
Worker 토큰·주소, 제공자 응답 원문은 싣지 않습니다. 화면이 자주 부르므로 전체 결과를 1초 동안 재사용합니다.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.local_access import require_local_access

from .prober import PROBE_URL_ENV, prober_from_env

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 3.0  # 각 하위 호출의 상한. 화면 조회가 멈추지 않게 합니다.
CACHE_TTL_S = 1.0
REQUEST_COUNT_KEYS = ("since", "write_total", "write_failed", "read_total", "read_failed")

SourceStatus = Literal["ok", "not_configured", "error"]
# skipped: 트래픽 게이트가 요청을 붙잡고 있거나(HOLD) 게이트 상태를 몰라 프로브를 보내지 않았습니다.
ProbeStatus = Literal["ok", "not_configured", "error", "skipped"]
PROBE_SAFE_MODES = ("SOURCE", "TARGET")
PROBE_SKIPPED_MESSAGE = "게이트가 HOLD 이거나 상태를 몰라 프로브를 보내지 않았습니다 (전환 측정에 섞이지 않게)"


class WorkerRequestCounts(BaseModel):
    since: str | None = None
    write_total: int | None = None
    write_failed: int | None = None
    read_total: int | None = None
    read_failed: int | None = None


class WorkerLive(BaseModel):
    status: SourceStatus
    error: str | None = None
    mode: str | None = None  # SOURCE | HOLD | TARGET
    next_origin: str | None = None
    hold_started_at: str | None = None
    released_at: str | None = None
    held_count: int | None = None
    inflight_source: int | None = None
    request_counts: WorkerRequestCounts | None = None
    isolated_nodes: list[str] | None = None  # Worker 보안 격리 상태의 노드 목록


class AgentLive(BaseModel):
    status: SourceStatus
    error: str | None = None
    kafka: bool | None = None
    connect: bool | None = None
    plugins_missing: list[str] | None = None


class ProbeLive(BaseModel):
    status: ProbeStatus
    error: str | None = None
    ok: bool | None = None  # 요청이 2xx·3xx 로 끝났는지. status=ok 이고 ok=false 면 서비스가 실패를 돌려준 것
    latency_ms: float | None = None
    origin: str | None = None  # Worker 의 X-Gate-Origin 헤더 (SOURCE | TARGET). 없으면 None


class EdgeSnapshot(BaseModel):
    checked_at: str  # ISO8601 UTC, "Z" 접미사
    worker: WorkerLive
    agent: AgentLive
    probe: ProbeLive


# ---------------------------------------------------------------------------
# 안전한 오류 문구
# ---------------------------------------------------------------------------


def _safe_error(exc: BaseException) -> str:
    """예외 종류와, 우리가 직접 만든 안전한 문구만 남깁니다. 제공자 본문·주소·토큰은 싣지 않습니다."""
    from app.features.db_migration.client import AgentError
    from app.features.provisioning.terraform import ProvisioningError

    name = type(exc).__name__
    if isinstance(exc, AgentError):
        return f"{name}: HTTP {exc.status_code}"
    if isinstance(exc, ProvisioningError):
        # WorkerClient 문구는 명령·HTTP 코드 뒤에 Worker 응답의 error 칸을 붙이므로 그 앞까지만 씁니다.
        text = str(exc)
        if "(HTTP " in text:
            text = text.split(")", 1)[0] + ")"
        return f"{name}: {text}"[:200]
    if isinstance(exc, (TimeoutError, FutureTimeout)):
        return f"TimeoutError: {CALL_TIMEOUT_S:g}초 안에 응답이 없습니다"
    return name


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _str_list_or_none(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


# ---------------------------------------------------------------------------
# 하위 조회
# ---------------------------------------------------------------------------


def _worker_live() -> WorkerLive:
    from app.features.provisioning.worker_client import WorkerClient

    try:
        client = WorkerClient.from_env()
    except Exception as exc:  # 토큰 누락(ProvisioningError)·토큰 파일 읽기 실패(OSError)
        return WorkerLive(status="error", error=_safe_error(exc))
    if client is None:
        return WorkerLive(status="not_configured")
    try:
        body = client.status(timeout_s=CALL_TIMEOUT_S)
    except Exception as exc:
        return WorkerLive(status="error", error=_safe_error(exc))
    if not isinstance(body, dict):
        return WorkerLive(status="error", error="Worker 상태 응답 형식이 올바르지 않습니다")

    counts = body.get("request_counts")
    request_counts = None
    if isinstance(counts, dict):
        request_counts = WorkerRequestCounts(
            since=_str_or_none(counts.get("since")),
            **{key: _int_or_none(counts.get(key)) for key in REQUEST_COUNT_KEYS if key != "since"},
        )
    quarantine = body.get("quarantine")
    isolated = _str_list_or_none(quarantine.get("isolated_nodes")) if isinstance(quarantine, dict) else None
    # 응답을 통째로 펼치지 않고 공개해도 되는 칸만 고릅니다 (target_url·reroute_target 같은 주소 제외).
    return WorkerLive(
        status="ok",
        mode=_str_or_none(body.get("mode")),
        next_origin=_str_or_none(body.get("next_origin")),
        hold_started_at=_str_or_none(body.get("hold_started_at")),
        released_at=_str_or_none(body.get("released_at")),
        held_count=_int_or_none(body.get("held_count")),
        inflight_source=_int_or_none(body.get("inflight_source")),
        request_counts=request_counts,
        isolated_nodes=isolated,
    )


def _agent_live() -> AgentLive:
    from app.features.db_migration.client import AgentSettings, CdcAgentClient

    from .runners import SIMULATION_UNSUPPORTED_MESSAGE

    try:
        settings = AgentSettings.from_env()
    except KeyError:  # CDC_AGENT_URL 없음 (MissingSetting)
        return AgentLive(status="not_configured")
    except Exception as exc:
        return AgentLive(status="error", error=_safe_error(exc))
    if settings.is_simulation:
        # 모의 에이전트의 '정상' 응답을 실제 상태처럼 보여 주지 않습니다 (runners._require_real_agent 와 같은 규칙).
        return AgentLive(status="error", error=SIMULATION_UNSUPPORTED_MESSAGE)
    settings = dataclasses.replace(settings, timeout_s=min(settings.timeout_s, CALL_TIMEOUT_S))
    agent = None
    try:
        agent = CdcAgentClient(settings)
        body = agent.status()
    except Exception as exc:
        return AgentLive(status="error", error=_safe_error(exc))
    finally:
        # 화면이 1초마다 새로 만드므로 연결 풀을 바로 닫습니다 (CdcAgentClient.close 는 job 종료 명령이라 쓰지 않음).
        if agent is not None:
            agent._http.close()
    if not isinstance(body, dict):
        return AgentLive(status="error", error="에이전트 상태 응답 형식이 올바르지 않습니다")
    return AgentLive(
        status="ok",
        kafka=_bool_or_none(body.get("kafka")),
        connect=_bool_or_none(body.get("connect")),
        plugins_missing=_str_list_or_none(body.get("plugins_missing")),
    )


# 화면 프로브는 공용 Worker(게이트)로 실제 요청을 보냅니다. 팀 규칙(11/8 박준서)대로 공용 Worker 를 테스트에 쓰지 않도록
# 기본은 끄고, 시연처럼 필요할 때만 ALPHA_LIVE_PROBE=1 로 켭니다. Worker 상태 읽기는 읽기 전용이라 그대로 둡니다.
LIVE_PROBE_ENV = "ALPHA_LIVE_PROBE"


def _live_probe_enabled() -> bool:
    return os.environ.get(LIVE_PROBE_ENV, "0") in ("1", "true", "True")


def _probe_live() -> ProbeLive:
    if not _live_probe_enabled():
        return ProbeLive(status="not_configured", error=f"{LIVE_PROBE_ENV} 가 꺼져 있어 화면 프로브를 보내지 않습니다")
    prober = prober_from_env()
    if prober is None:
        if os.environ.get(PROBE_URL_ENV, "").strip():
            return ProbeLive(status="error", error=f"{PROBE_URL_ENV} 값을 쓸 수 없습니다")
        return ProbeLive(status="not_configured")
    try:
        result = prober.probe_once(timeout_s=CALL_TIMEOUT_S)
    except Exception as exc:
        return ProbeLive(status="error", error=_safe_error(exc))
    return ProbeLive(
        status="ok",
        ok=_bool_or_none(result.get("ok")),
        latency_ms=result.get("latency_ms") if isinstance(result.get("latency_ms"), (int, float)) else None,
        origin=_str_or_none(result.get("origin")),
    )


# ---------------------------------------------------------------------------
# 묶음 + 캐시
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _probe_allowed(worker: WorkerLive) -> bool:
    """프로브는 실제 게이트를 지나는 요청입니다. HOLD 중에 보내면 held_count·read_total 에 섞여 전환 증적을 흐립니다.

    Worker 가 없으면 붙잡힐 일이 없어 보냅니다. Worker 가 SOURCE·TARGET 으로 확인될 때만 보내고,
    HOLD 이거나 상태를 모르면(오류) 보내지 않습니다.
    """
    if worker.status == "not_configured":
        return True
    return worker.status == "ok" and worker.mode in PROBE_SAFE_MODES


def _result(future: Any, model: Any, key: str, deadline: float) -> Any:
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout as exc:
        return model(status="error", error=_safe_error(exc))
    except Exception as exc:  # 하위 함수가 놓친 예외도 화면을 깨지 않게 합니다
        log.warning("실시간 엣지 조회 %s 실패 (%s)", key, type(exc).__name__)
        return model(status="error", error=_safe_error(exc))


def _build_snapshot() -> EdgeSnapshot:
    """Worker·에이전트를 동시에 읽고, Worker 상태를 본 뒤에만 프로브를 보냅니다. 각 호출에 상한을 겁니다."""
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="live-edge")
    try:
        worker_future = pool.submit(_worker_live)
        agent_future = pool.submit(_agent_live)
        deadline = time.monotonic() + CALL_TIMEOUT_S + 0.5
        worker = _result(worker_future, WorkerLive, "worker", deadline)
        if _probe_allowed(worker):
            probe = _result(pool.submit(_probe_live), ProbeLive, "probe", time.monotonic() + CALL_TIMEOUT_S + 0.5)
        else:
            probe = ProbeLive(status="skipped", error=PROBE_SKIPPED_MESSAGE)
        agent = _result(agent_future, AgentLive, "agent", deadline)
    finally:
        pool.shutdown(wait=False)
    return EdgeSnapshot(checked_at=_now_iso(), worker=worker, agent=agent, probe=probe)


_clock = time.monotonic  # 테스트에서 바꿉니다
_cache_lock = threading.Lock()
_cached: tuple[float, EdgeSnapshot] | None = None


def edge_snapshot() -> EdgeSnapshot:
    """1초 안의 재호출은 같은 객체를 돌려줍니다. 잠금을 쥔 채 새로 만들어 동시 요청이 겹쳐 부르지 않게 합니다."""
    global _cached
    with _cache_lock:
        now = _clock()
        if _cached is not None and now - _cached[0] < CACHE_TTL_S:
            return _cached[1]
        snapshot = _build_snapshot()
        _cached = (_clock(), snapshot)
        return snapshot


def reset_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


router = APIRouter(prefix="/live", tags=["live"], dependencies=[Depends(require_local_access)])


@router.get("/edge", response_model=EdgeSnapshot)
def get_edge() -> EdgeSnapshot:
    """Worker 게이트·CDC 에이전트·외부 프로버의 지금 상태. 읽기 전용이며 값을 지어내지 않습니다."""
    return edge_snapshot()
