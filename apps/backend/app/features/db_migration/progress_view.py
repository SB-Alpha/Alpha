"""모듈이 밖으로 주는 진행값. 클라우드 이름 대신 출발지·목적지 역할로 말한다.

보고가 없거나 형식이 틀린 값은 넣지 않는다. 받는 쪽은 없는 값을 "미제공"으로 보여 준다.
"""

import ipaddress
import logging
import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from .schemas import CutoverOutcome

logger = logging.getLogger(__name__)

READY_PHASE = "cutover_ready"
CUTOVER_PHASE = "cutover"
STABILIZING_PHASE = "stabilizing"
CLOSED_PHASE = "closed"
SWITCHED_PHASES = {STABILIZING_PHASE, CLOSED_PHASE}

SWITCHED_STEP = "switched_to_target"
CUTOVER_SEQUENCE = ("hold", "marker_on_target", "snapshot_fixed", SWITCHED_STEP, "released")
# 이전 에이전트는 snapshot_fixed 자리에 validated를 남긴다.
_STEP_ALIASES = {"snapshot_fixed": ("snapshot_fixed", "validated")}
_STEP_NUMBERS = ("elapsed_ms", "waited_ms", "mismatch")
_COUNT_METRICS = ("replication_lag_bytes", "mismatch", "rows_checked")
_VERIFICATIONS = {"running", "match", "mismatch", "unverifiable", "unknown"}
# 004의 11·12번. 전환부터는 단계가 아니라 phase와 전환 값을 본다.
_AGENT_STEPS = {"source_prepare": 11, "target_prepare": 12, "precheck": 12, "replication_setup": 12,
                "snapshot": 12, "streaming": 12, READY_PHASE: 12}


def step_of_phase(phase: Any) -> int | None:
    return _AGENT_STEPS.get(phase)


def still_on_source(job: Mapping[str, Any]) -> bool:
    """전환을 시도한 적이 없을 때만 원본이라고 말한다. 시도했다면 판단은 전환 결과가 맡는다."""
    return job.get("phase") in _AGENT_STEPS and not _cutover_evidence(job)


def clamp_progress(value: Any) -> int:
    return min(100, max(0, int(value))) if _is_number(value) else 0


def network_exposure(address: Any) -> Literal["PRIVATE", "PUBLIC"] | None:
    """IP가 아니면 알 수 없으므로 None이다."""
    try:
        return "PRIVATE" if ipaddress.ip_address(address).is_private else "PUBLIC"
    except (TypeError, ValueError):
        return None


def classify_cutover(job: Mapping[str, Any], confirm_source: Callable[[], bool] | None = None) -> CutoverOutcome | None:
    """전환 결과. 아직 판정할 수 없으면 None이다.

    경로 변경 응답을 잃으면 에이전트는 경로가 바뀐 줄 모르고 복귀를 보고할 수 있다.
    그래서 호출자가 원본 위치를 확인해 주지 않으면 rolled_back으로 답하지 않는다.
    """
    state, phase = job.get("state"), job.get("phase")
    if state == "failed" and phase == CUTOVER_PHASE:
        return "post_switch_failed"
    if state in ("failed", "aborted"):
        return "unknown"
    if phase in (STABILIZING_PHASE, CLOSED_PHASE):
        return "switched"
    names = _step_names(job)
    if phase != READY_PHASE or "failed" not in names:
        return None
    if SWITCHED_STEP in names or any(name.endswith("_failed") for name in names):
        return "unknown"
    return "rolled_back" if _source_confirmed(confirm_source) else "unknown"


def to_metrics(job: Mapping[str, Any], clean: Callable[[str], str] = str) -> dict[str, Any]:
    """에이전트 작업을 제공 값으로 바꾼다. clean은 에이전트 오류 문구의 비밀값을 지운다."""
    raw = job.get("metrics")
    raw = raw if isinstance(raw, Mapping) else {}
    metrics: dict[str, Any] = {key: raw[key] for key in _COUNT_METRICS if _is_count(raw.get(key))}
    if _is_number(raw.get("copy_percent")) and 0 <= raw["copy_percent"] <= 100:
        metrics["copy_percent"] = raw["copy_percent"]
    if _is_aware_time(raw.get("measured_at")):
        metrics["measured_at"] = raw["measured_at"]
    if tables := _tables(raw.get("tables")):
        metrics["tables"] = tables
    if raw.get("verification") in _VERIFICATIONS:
        metrics["verification"] = raw["verification"]
    if raw.get("simulated") is True:
        metrics["simulated"] = True
    if isinstance(raw.get("measure_error"), str) and raw["measure_error"].strip():
        metrics["measure_error"] = clean(raw["measure_error"])
    if raw.get("traffic_mode") == "TARGET":
        metrics["serving"] = "target"
    if _is_number(raw.get("cutover_ms")) and raw["cutover_ms"] >= 0:
        metrics.update(cutover_seconds=round(raw["cutover_ms"] / 1000, 3), cutover_seconds_source="agent")
    if isinstance(job.get("phase"), str):
        metrics["phase"] = job["phase"]
    if job.get("state") == "failed" and isinstance(job.get("message"), str) and job["message"].strip():
        metrics["cdc_error"] = clean(job["message"])
    metrics.update(_cutover_values(job, clean))
    return metrics


def _cutover_values(job: Mapping[str, Any], clean: Callable[[str], str]) -> dict[str, Any]:
    # 전환 증적이 없으면 전환 전 실패이므로 전환 결과가 아니다.
    if not _cutover_evidence(job):
        return {}
    values: dict[str, Any] = {"cutover_steps": _steps(job, clean)}
    outcome = classify_cutover(job)
    if outcome is not None:
        values["cutover_outcome"] = outcome
    if outcome == "switched":
        missing = _missing_steps(_step_names(job))
        values["cutover_sequence"] = "INCOMPLETE" if missing else "VERIFIED"
        if missing:
            values["cutover_sequence_missing"] = missing
    return values


def _steps(job: Mapping[str, Any], clean: Callable[[str], str]) -> list[dict[str, Any]]:
    steps = []
    for item in _raw_steps(job):
        step: dict[str, Any] = {"step": item["step"]}
        step.update({key: item[key] for key in _STEP_NUMBERS if _is_number(item.get(key))})
        if isinstance(item.get("error"), str) and item["error"].strip():
            step["error"] = clean(item["error"])
        steps.append(step)
    return steps


def _missing_steps(names: list[str]) -> list[str]:
    missing = []
    position = 0
    for expected in CUTOVER_SEQUENCE:
        accepted = _STEP_ALIASES.get(expected, (expected,))
        found = next((i for i in range(position, len(names)) if names[i] in accepted), None)
        if found is None:
            missing.append(expected)
        else:
            position = found + 1
    return missing


def _tables(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    tables = {}
    for name, row in raw.items():
        if not isinstance(row, Mapping) or not _is_count(row.get("source_rows")) or not _is_count(row.get("target_rows")):
            continue
        table = {"source_rows": row["source_rows"], "target_rows": row["target_rows"]}
        if _is_number(row.get("percent")):
            table["percent"] = row["percent"]
        if isinstance(row.get("match"), bool):
            table["match"] = row["match"]
        tables[name] = table
    return tables


def _cutover_evidence(job: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = job.get("evidence")
    cutover = evidence.get("cutover") if isinstance(evidence, Mapping) else None
    return cutover if isinstance(cutover, Mapping) else {}


def _raw_steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = _cutover_evidence(job).get("steps")
    if not isinstance(steps, list):
        return []
    return [item for item in steps if isinstance(item, Mapping) and isinstance(item.get("step"), str)]


def _step_names(job: Mapping[str, Any]) -> list[str]:
    return [item["step"] for item in _raw_steps(job)]


def _source_confirmed(confirm_source: Callable[[], bool] | None) -> bool:
    if confirm_source is None:
        return False
    try:
        return confirm_source() is True
    except Exception as exc:  # noqa: BLE001 - 확인하지 못한 것은 원본에 있다는 뜻이 아니다
        logger.warning("could not confirm the source route (%s)", type(exc).__name__)
        return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_aware_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False
