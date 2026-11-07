"""전환 승인 ② 판정. run 상태만으로 계산합니다. 값이 없으면 통과로 치지 않고 unknown 입니다.

화면 표시와 승인 API 가 이 판정 하나를 씁니다.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

from .schemas import CheckStatus, CutoverDecision, CutoverReadiness, ReadinessCheck, RunStatus, StageName

# 임시 기준값입니다. 팀 합의 후 바꿉니다.
MAX_REPLICATION_LAG_BYTES = 1024 * 1024
# 복제 지연 값이 이보다 오래 갱신되지 않으면 지금 값으로 보지 않습니다 (확인 중).
# 기준은 에이전트가 지표를 다시 재는 주기(cdc_agent config METRICS_INTERVAL_S)의 두 번입니다. 승인 대기
# (cutover_ready) 중에는 백엔드가 더 자주 물어도 에이전트가 그 주기마다만 재므로, 주기보다 짧은 기준은 판정이
# 오락가락합니다 (softwareDefine 실측: 30초 주기에 20초 기준이면 28% 가 확인 중). 한 번 놓쳐도 흔들리지 않고,
# a333af6e 처럼 30초 전 값으로 "가능"을 내지 않으려면 에이전트 주기를 10초로 두고 기준 20초를 씁니다.
# 에이전트 주기를 바꾸지 못한 환경은 CDC_AGENT_METRICS_INTERVAL_S 에 에이전트와 같은 값을 넣습니다 (기준도 두 배로 늘어남).
AGENT_METRICS_INTERVAL_ENV = "CDC_AGENT_METRICS_INTERVAL_S"
DEFAULT_AGENT_METRICS_INTERVAL_S = 10.0


def max_lag_age_s() -> float:
    """지연 값이 지금 값으로 인정되는 최대 나이 (초). 에이전트 측정 주기의 두 번입니다."""
    try:
        interval = float(os.getenv(AGENT_METRICS_INTERVAL_ENV, DEFAULT_AGENT_METRICS_INTERVAL_S))
    except ValueError:
        interval = DEFAULT_AGENT_METRICS_INTERVAL_S
    if not math.isfinite(interval) or interval <= 0:
        interval = DEFAULT_AGENT_METRICS_INTERVAL_S
    return 2 * interval

_NOT_PROVIDED = "미제공"


def _find_metric(run: RunStatus, key: str) -> Any:
    """뒤 단계부터 찾습니다. 같은 키를 여러 단계가 보고하면 가장 최근 단계 값을 씁니다.

    전환 단계 지표는 보지 않습니다. 지난 시도의 마지막 값이라 갱신되지 않아, 지금 값을 가리면 판정이 굳습니다.
    """
    for stage in reversed(run.stages):
        if stage.stage != StageName.CUTOVER and key in stage.metrics:
            return stage.metrics[key]
    return None


def _find_with_stage(run: RunStatus, key: str) -> tuple[Any, dict[str, Any]]:
    """_find_metric 과 같은 순서로 찾고, 값을 보고한 단계의 지표도 함께 돌려줍니다."""
    for stage in reversed(run.stages):
        if stage.stage != StageName.CUTOVER and key in stage.metrics:
            return stage.metrics[key], stage.metrics
    return None, {}


def _aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _lag_check(run: RunStatus, now: datetime) -> ReadinessCheck:
    """지연 값과 같은 보고에 실린 measured_at 을 함께 봅니다.

    단계 지표는 마지막 보고 값이 남아 있어, 갱신이 끊긴 사이 지연이 커져도 예전 값으로 통과할 수 있습니다
    (s5 a333af6e: 30초 전 400 bytes 로 통과, 승인 순간 실제 48,555,488 bytes). 오래된 값은 통과로 치지 않고
    확인 중(unknown)으로 둡니다. 기준을 넘는 값은 오래됐어도 막습니다. measured_at 이 없는 예전 보고는 값만 봅니다.
    """
    base = {"id": "lag", "title": "복제 지연"}
    raw, stage_metrics = _find_with_stage(run, "replication_lag_bytes")
    value = _as_number(raw)
    if value is None:
        return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)
    detail = f"{value:,.0f} bytes (기준 {MAX_REPLICATION_LAG_BYTES:,} 이하, 임시)"
    if value > MAX_REPLICATION_LAG_BYTES:
        return ReadinessCheck(**base, status="fail", detail=detail)
    measured = _aware_time(stage_metrics.get("measured_at"))
    if measured is None:
        return ReadinessCheck(**base, status="pass", detail=f"{detail} · 측정 시각 미제공")
    age = (now - measured).total_seconds()
    limit = max_lag_age_s()
    if age > limit:
        return ReadinessCheck(
            **base,
            status="unknown",
            detail=f"확인 중: 마지막 측정 {age:.0f}초 전 ({value:,.0f} bytes, 기준 {limit:.0f}초 이내 값만 통과)",
        )
    return ReadinessCheck(**base, status="pass", detail=f"{detail} · {max(age, 0):.0f}초 전 측정")


def _is_verified_by_agent(run: RunStatus) -> bool:
    """에이전트가 복사와 일치를 확인해 cutover_ready 로 올렸는지.

    쓰기 중에는 두 DB 를 다른 순간에 읽어 복사율·불일치 값이 오갑니다. 이 뒤의 순간값으로는 막지 않습니다.
    쓰기를 멈춘 전환 기준점에서 다시 엄격히 검사해 다르면 전환하지 않습니다.
    """
    return _find_metric(run, "phase") == "cutover_ready"


def _copy_check(run: RunStatus) -> ReadinessCheck:
    base = {"id": "copy", "title": "초기 복사"}
    value = _as_number(_find_metric(run, "copy_percent"))
    if value is None:
        return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)
    if value >= 100:
        return ReadinessCheck(**base, status="pass", detail=f"{value:g}%")
    if _is_verified_by_agent(run):
        return ReadinessCheck(**base, status="pass", detail=f"복사 완료, 쓰기 반영 중 {value:g}%")
    return ReadinessCheck(**base, status="fail", detail=f"{value:g}%")


def _mismatch_check(run: RunStatus) -> ReadinessCheck:
    # 전환 전 지표는 행 수만 비교합니다. 내용 비교는 전환 시점의 verification 입니다.
    base = {"id": "mismatch", "title": "행 수 불일치 테이블"}
    value = _as_number(_find_metric(run, "mismatch"))
    if value is None:
        return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)
    if value == 0:
        return ReadinessCheck(**base, status="pass", detail="0개")
    if _is_verified_by_agent(run):
        return ReadinessCheck(**base, status="pass", detail=f"순간 차이 {value:g}개. 전환 기준점에서 최종 검사합니다")
    return ReadinessCheck(**base, status="fail", detail=f"{value:g}개")


def _cdc_check(run: RunStatus) -> ReadinessCheck:
    base = {"id": "cdc", "title": "복제 상태"}
    error = _find_metric(run, "cdc_error")
    if error:
        return ReadinessCheck(**base, status="fail", detail=str(error))
    phase = _find_metric(run, "phase")
    if phase:
        return ReadinessCheck(**base, status="pass", detail=f"복제 단계: {phase}")
    return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)


def _app_health_check(run: RunStatus) -> ReadinessCheck:
    base = {"id": "app_health", "title": "목적지 앱 상태"}
    # 수동 입력값은 실제 확인 결과가 아니므로 통과나 실패로 치지 않습니다.
    if run.stage(StageName.APP_DEPLOY).manual:
        return ReadinessCheck(**base, status="unknown", detail="수동 입력이라 확인 안 됨")
    output = run.outputs.get(StageName.APP_DEPLOY.value)
    healthy = output.get("healthy") if isinstance(output, dict) else None
    if healthy is True:
        return ReadinessCheck(**base, status="pass", detail="/health/ready 200")
    if healthy is False:
        return ReadinessCheck(**base, status="fail", detail="상태 확인 실패")
    return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)


def _decision(checks: list[ReadinessCheck]) -> CutoverDecision:
    """실패가 하나라도 있으면 막습니다. 미제공만 있으면 운영자의 명시적 확인이 있어야 엽니다."""
    statuses = {c.status for c in checks}
    if "fail" in statuses:
        return "no_go"
    return "needs_confirmation" if "unknown" in statuses else "go"


def cutover_readiness(run: RunStatus, *, now: datetime | None = None) -> CutoverReadiness:
    now = now or datetime.now(timezone.utc)
    checks = [
        _cdc_check(run),
        _copy_check(run),
        _lag_check(run, now),
        _mismatch_check(run),
        _app_health_check(run),
    ]
    return CutoverReadiness(decision=_decision(checks), checks=checks)
