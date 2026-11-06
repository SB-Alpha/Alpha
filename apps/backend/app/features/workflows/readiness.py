"""전환 승인 ② 판정. run 상태만으로 계산합니다. 값이 없으면 통과로 치지 않고 unknown 입니다.

화면 표시와 승인 API 가 이 판정 하나를 씁니다.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .schemas import CheckStatus, CutoverDecision, CutoverReadiness, ReadinessCheck, RunStatus, StageName

# 임시 기준값입니다. 팀 합의 후 바꿉니다.
MAX_REPLICATION_LAG_BYTES = 1024 * 1024

_NOT_PROVIDED = "미제공"


def _find_metric(run: RunStatus, key: str) -> Any:
    """뒤 단계부터 찾습니다. 같은 키를 여러 단계가 보고하면 가장 최근 단계 값을 씁니다."""
    for stage in reversed(run.stages):
        if key in stage.metrics:
            return stage.metrics[key]
    return None


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


def _metric_check(
    run: RunStatus,
    check_id: str,
    title: str,
    key: str,
    is_pass: Callable[[float], bool],
    describe: Callable[[float], str],
) -> ReadinessCheck:
    value = _as_number(_find_metric(run, key))
    if value is None:
        return ReadinessCheck(id=check_id, title=title, status="unknown", detail=_NOT_PROVIDED)
    status: CheckStatus = "pass" if is_pass(value) else "fail"
    return ReadinessCheck(id=check_id, title=title, status=status, detail=describe(value))


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


def cutover_readiness(run: RunStatus) -> CutoverReadiness:
    checks = [
        _cdc_check(run),
        _metric_check(run, "copy", "초기 복사", "copy_percent", lambda v: v >= 100, lambda v: f"{v:g}%"),
        _metric_check(
            run,
            "lag",
            "복제 지연",
            "replication_lag_bytes",
            lambda v: v <= MAX_REPLICATION_LAG_BYTES,
            lambda v: f"{v:,.0f} bytes (기준 {MAX_REPLICATION_LAG_BYTES:,} 이하, 임시)",
        ),
        _metric_check(run, "mismatch", "불일치 테이블", "mismatch", lambda v: v == 0, lambda v: f"{v:g}개"),
        _app_health_check(run),
    ]
    return CutoverReadiness(decision=_decision(checks), checks=checks)
