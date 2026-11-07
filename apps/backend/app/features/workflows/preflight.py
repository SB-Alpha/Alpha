"""전환 승인 ② 전 점검. 참고용이며 승인을 막지 않습니다.

잴 수 없는 값은 unknown 으로 둡니다. 호스트·주소·토큰·예외 원문은 결과에 싣지 않고, 로그에도 예외 종류만 남깁니다.
"""

from __future__ import annotations

import logging
import math
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .context import now
from .runners import _agent_is_simulated, _aware_datetime
from .schemas import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
    PreflightVerdict,
    RunStatus,
    StageName,
)

log = logging.getLogger(__name__)

MAX_LAG_ENV = "ALPHA_CUTOVER_MAX_LAG_BYTES"
READINESS_PATH = "/health/ready"  # provisioning.deploy_app 이 확인한 경로와 같습니다
HTTP_TIMEOUT_S = 5
# 파이썬 기본 User-Agent 는 Cloudflare 가 봇으로 보고 막습니다.
USER_AGENT = "alpha-workflows/1.0"

_FROM_ENV: Any = object()  # max_lag_bytes 를 넘기지 않으면 환경변수를 봅니다
_MAX_DETAIL_FIELD = 40

_LABELS = {
    "job_ready": "CDC 작업 준비",
    "db_connectivity": "DB 연결",
    "data_match": "데이터 일치",
    "replication_lag": "복제 지연",
    "target_ready": "목적지 앱 준비",
    "worker_target": "Worker 전환 대상",
}
_UNKNOWN_DETAIL = "확인하지 못했습니다"
_NO_AGENT_DETAIL = "CDC 에이전트에 연결하지 못해 확인하지 못했습니다"
_MEASURE_FAILED_DETAIL = "최근 측정이 실패해 값을 믿을 수 없습니다"
_NOT_MEASURED_DETAIL = "측정값이 없습니다"
_NO_MEASURED_TIME_DETAIL = "유효한 측정 시각이 없습니다"
_NO_APP_URL_DETAIL = "앱 주소가 없습니다"

HttpGet = Callable[[str], int]


def _result(
    check_id: str,
    status: PreflightStatus,
    detail: str | None = None,
    value: int | str | None = None,
) -> PreflightCheck:
    return PreflightCheck(id=check_id, label=_LABELS[check_id], status=status, detail=detail, value=value)


def _guarded(check_id: str, check: Callable[[], PreflightCheck]) -> PreflightCheck:
    try:
        return check()
    except Exception as exc:
        log.warning("전환 전 점검 %s 실패 (%s)", check_id, type(exc).__name__)
        return _result(check_id, "unknown", _UNKNOWN_DETAIL)


def _count(value: Any) -> int | None:
    """0 이상의 정수만 받습니다. bool 과 소수는 측정값으로 보지 않습니다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value) if value >= 0 else None


def _measure_failed(metrics: Mapping[str, Any]) -> bool:
    return metrics.get("measure_error") is not None


# -- 에이전트 값 ------------------------------------------------------------------


def _default_get_job(job_id: str) -> Any:
    from app.features.db_migration.client import AgentSettings, CdcAgentClient

    return CdcAgentClient(AgentSettings.from_env()).get_job(job_id)


def _load_job(run_id: str, get_job: Callable[[str], Any] | None) -> Mapping[str, Any] | None:
    from app.features.db_migration.guide_workflow import to_job_id

    try:
        job = (get_job or _default_get_job)(to_job_id(run_id))
    except Exception as exc:
        log.warning("전환 전 점검: 에이전트 조회 실패 (%s)", type(exc).__name__)
        return None
    return job if isinstance(job, Mapping) else None


def _check_job_ready(job: Mapping[str, Any]) -> PreflightCheck:
    state, phase = job.get("state"), job.get("phase")
    if state == "waiting_approval" and phase == "cutover_ready":
        return _result("job_ready", "pass")
    detail = f"현재 상태 {str(state)[:_MAX_DETAIL_FIELD]}, 단계 {str(phase)[:_MAX_DETAIL_FIELD]}"
    return _result("job_ready", "fail", detail)


def _check_db_connectivity(metrics: Mapping[str, Any]) -> PreflightCheck:
    # 에이전트의 비교 측정은 양쪽 DB 에 로그인해야 하므로 측정 성공이 곧 연결 확인입니다.
    if _measure_failed(metrics):
        return _result("db_connectivity", "fail", "에이전트가 DB 측정에 실패했습니다")
    # 합의된 유효 기간이 없어 시각의 나이는 보지 않습니다.
    if _aware_datetime(metrics.get("measured_at")) is not None:
        return _result("db_connectivity", "pass")
    return _result("db_connectivity", "unknown", _NO_MEASURED_TIME_DETAIL)


def _check_data_match(metrics: Mapping[str, Any]) -> PreflightCheck:
    if _measure_failed(metrics):
        return _result("data_match", "unknown", _MEASURE_FAILED_DETAIL)
    mismatch = _count(metrics.get("mismatch"))
    if mismatch is None:
        return _result("data_match", "unknown", _NOT_MEASURED_DETAIL)
    if mismatch > 0:
        return _result("data_match", "fail", "원본과 목적지가 다른 테이블이 있습니다", mismatch)
    return _result("data_match", "pass", value=0)


def _lag_limit(max_lag_bytes: Any) -> int | None:
    if max_lag_bytes is not _FROM_ENV:
        return max_lag_bytes
    raw = os.environ.get(MAX_LAG_ENV, "").strip()
    if not raw:
        return None
    limit = int(raw)
    if limit < 0:
        raise ValueError(MAX_LAG_ENV)
    return limit


def _check_replication_lag(metrics: Mapping[str, Any], max_lag_bytes: Any) -> PreflightCheck:
    if _measure_failed(metrics):
        return _result("replication_lag", "unknown", _MEASURE_FAILED_DETAIL)
    lag = _count(metrics.get("replication_lag_bytes"))
    if lag is None:
        return _result("replication_lag", "unknown", _NOT_MEASURED_DETAIL)
    try:
        limit = _lag_limit(max_lag_bytes)
    except ValueError:
        return _result("replication_lag", "unknown", f"{MAX_LAG_ENV} 값이 잘못되었습니다", lag)
    if limit is None:
        return _result("replication_lag", "info", "참고값입니다. 승인 기준은 승인 조건의 복제 지연 항목이 정합니다", lag)
    if lag > limit:
        return _result("replication_lag", "fail", f"기준 {limit} 바이트를 넘었습니다", lag)
    return _result("replication_lag", "pass", f"기준 {limit} 바이트 이내입니다", lag)


# -- 목적지 앱·Worker --------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _http_status(url: str) -> int:
    """응답 코드를 돌려줍니다. 리디렉션은 따라가지 않고, 연결 실패는 예외로 둡니다 (코드를 만들지 않습니다)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def _app_url(run: RunStatus) -> str | None:
    output = run.outputs.get(StageName.APP_DEPLOY.value)
    url = output.get("app_url") if isinstance(output, Mapping) else None
    return url if isinstance(url, str) and url else None


def _is_callable_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
    except ValueError:
        return False


def _check_target_ready(run: RunStatus, http_get: HttpGet | None) -> PreflightCheck:
    url = _app_url(run)
    if url is None:
        return _result("target_ready", "unknown", _NO_APP_URL_DETAIL)
    # 수동 입력은 임의의 값을 저장할 수 있으므로 그 주소로는 요청을 보내지 않습니다 (SSRF 방지).
    if run.stage(StageName.APP_DEPLOY).manual or not _is_callable_url(url):
        return _result("target_ready", "unknown", "수동 입력이거나 https 주소가 아니어서 호출하지 않았습니다")
    status = (http_get or _http_status)(url.rstrip("/") + READINESS_PATH)
    if status == 200:
        return _result("target_ready", "pass", value=status)
    return _result("target_ready", "fail", "준비 상태 응답이 200 이 아닙니다", status)


def _default_worker() -> Any:
    from app.features.provisioning.worker_client import WorkerClient

    return WorkerClient.from_env()


def _check_worker_target(run: RunStatus, worker_target: Callable[[], Any] | None) -> PreflightCheck:
    url = _app_url(run)
    if url is None:
        return _result("worker_target", "unknown", _NO_APP_URL_DETAIL)
    worker = (worker_target or _default_worker)()
    if worker is None:
        return _result("worker_target", "unknown", "Worker 주소가 설정되지 않았습니다")
    stored = worker.get_target()
    if stored is None:
        # 저장된 주소가 없으면 Worker 는 설정된 기본 대상으로 보냅니다. 그 주소는 여기서 알 수 없습니다.
        return _result("worker_target", "unknown", "저장된 전환 대상이 없어 Worker 가 설정된 기본 대상을 쓰고 있습니다")
    if isinstance(stored, str) and stored.rstrip("/") == url.rstrip("/"):
        return _result("worker_target", "pass")
    return _result("worker_target", "fail", "Worker 의 전환 대상이 배포된 앱과 다릅니다")


# -- 종합 --------------------------------------------------------------------------


def _agent_checks(job: Mapping[str, Any] | None, max_lag_bytes: Any) -> list[PreflightCheck]:
    metrics = job.get("metrics") if job is not None else None
    values: Mapping[str, Any] = metrics if isinstance(metrics, Mapping) else {}
    checks: list[tuple[str, Callable[[], PreflightCheck]]] = [
        ("job_ready", lambda: _check_job_ready(job)),
        ("db_connectivity", lambda: _check_db_connectivity(values)),
        ("data_match", lambda: _check_data_match(values)),
        ("replication_lag", lambda: _check_replication_lag(values, max_lag_bytes)),
    ]
    if job is None:
        return [_result(check_id, "unknown", _NO_AGENT_DETAIL) for check_id, _ in checks]
    return [_guarded(check_id, check) for check_id, check in checks]


def _verdict(checks: list[PreflightCheck], simulated: bool) -> PreflightVerdict:
    # 모의 에이전트는 실제 전환 준비를 보여 주지 못하므로 go 도 no_go 도 말하지 않습니다.
    if simulated:
        return "unverified"
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "no_go"
    return "unverified" if "unknown" in statuses else "go"


def evaluate_cutover(
    run: RunStatus,
    *,
    get_job: Callable[[str], Any] | None = None,
    http_get: HttpGet | None = None,
    worker_target: Callable[[], Any] | None = None,
    max_lag_bytes: int | None = _FROM_ENV,
) -> PreflightReport:
    """지금 전환해도 되는지 점검합니다. 각 점검은 따로 보호되어, 하나가 실패해도 나머지는 계속합니다."""
    checks = [
        *_agent_checks(_load_job(run.run_id, get_job), max_lag_bytes),
        _guarded("target_ready", lambda: _check_target_ready(run, http_get)),
        _guarded("worker_target", lambda: _check_worker_target(run, worker_target)),
    ]
    simulated = _agent_is_simulated()
    return PreflightReport(
        run_id=run.run_id,
        verdict=_verdict(checks, simulated),
        simulated=simulated,
        checks=checks,
        measured_at=now(),
    )
