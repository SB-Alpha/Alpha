"""전환 승인 ② 판정. run 상태만으로 계산합니다. 값이 없으면 통과로 치지 않고 unknown 입니다.

화면 표시와 승인 API 가 이 판정 하나를 씁니다.
"""

from __future__ import annotations

import math
from typing import Any

from .schemas import CheckStatus, CutoverDecision, CutoverReadiness, ReadinessCheck, RunStatus, StageName

# 임시 기준값입니다. 팀 합의 후 바꿉니다.
MAX_REPLICATION_LAG_BYTES = 1024 * 1024

_NOT_PROVIDED = "미제공"


def _find_metric(run: RunStatus, key: str) -> Any:
    """뒤 단계부터 찾습니다. 같은 키를 여러 단계가 보고하면 가장 최근 단계 값을 씁니다.

    전환 단계 지표는 보지 않습니다. 지난 시도의 마지막 값이라 갱신되지 않아, 지금 값을 가리면 판정이 굳습니다.
    """
    for stage in reversed(run.stages):
        if stage.stage != StageName.CUTOVER and key in stage.metrics:
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


def _lag_check(run: RunStatus) -> ReadinessCheck:
    base = {"id": "lag", "title": "복제 지연"}
    value = _as_number(_find_metric(run, "replication_lag_bytes"))
    if value is None:
        return ReadinessCheck(**base, status="unknown", detail=_NOT_PROVIDED)
    status: CheckStatus = "pass" if value <= MAX_REPLICATION_LAG_BYTES else "fail"
    detail = f"{value:,.0f} bytes (기준 {MAX_REPLICATION_LAG_BYTES:,} 이하, 임시)"
    return ReadinessCheck(**base, status=status, detail=detail)


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
    base = {"id": "mismatch", "title": "불일치 테이블"}
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


def cutover_readiness(run: RunStatus) -> CutoverReadiness:
    checks = [
        _cdc_check(run),
        _copy_check(run),
        _lag_check(run),
        _mismatch_check(run),
        _app_health_check(run),
    ]
    return CutoverReadiness(decision=_decision(checks), checks=checks)
