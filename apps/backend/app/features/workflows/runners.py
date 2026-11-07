"""단계별 모듈 연결. 각 runner 는 run 상태에서 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 부릅니다.

모듈은 자기 `schemas.py` 와 진입 함수 `service.run` 만 씁니다 (확정서 2절).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .context import RunAborted
from .engine import CUTOVER_ON_SOURCE, CUTOVER_OUTCOME, StageRunner
from .prober import ProbedContext, prober_from_env
from .schemas import RunContext, RunStatus, StageName, WorkflowState
from .target_adapters import adapter_for, provider_of

log = logging.getLogger(__name__)

# 전환 순서. 실행은 에이전트가 하고 workflows 는 증적으로 확인만 합니다 (PR #45).
CUTOVER_SEQUENCE = ("hold", "marker_on_target", "snapshot_fixed", "switched_to_target", "released")
# 이전 에이전트는 보류 중에 비교를 끝내고 snapshot_fixed 자리에 validated 를 남깁니다.
_CUTOVER_STEP_ALIASES = {"snapshot_fixed": ("snapshot_fixed", "validated")}
_STEP_NUMBER_KEYS = ("elapsed_ms", "waited_ms", "mismatch")
_MAX_AGENT_TEXT = 300
_MAX_AGENT_TEXT_SCANNED = 4000  # 아주 긴 값에 정규식이 오래 걸리지 않게 먼저 자릅니다
_DEFAULT_CUTOVER_PROGRESS = 75
_SWITCHED_PHASES = {"stabilizing", "closed"}
_PAST_CUTOVER_PHASES = {"cutover", *_SWITCHED_PHASES}
VERIFICATION_RUNNING = "running"
VERIFICATION_UNKNOWN = "unknown"  # workflows 가 기다리는 동안 결과를 받지 못했습니다. 에이전트 값이 아닙니다.
# 에이전트의 비교 제한(VERIFY_TIMEOUT_S 기본 3600초)보다 조금 길게 기다립니다.
VERIFICATION_WAIT_S = 3700.0

PROBE_SETTLE_S = 3.0
# CDC 스트리밍 진입까지 기다리는 전체 상한. 실제 클라우드에서는 VPN 생성과 터널 협상만으로 10분을 넘길 수 있습니다.
# db_migration 의 각 단계에는 자기 상한이 있어서(VPN Terraform·터널 대기·접속 확인), 그 작업이 살아 있는 동안은
# 이 상한까지 기다립니다.
CDC_READY_TIMEOUT_S = 1800.0


class MissingStageOutput(RuntimeError):
    """요청이나 앞 단계 출력이 없어 이 단계 입력을 만들 수 없을 때."""


def _secret_encryption_metric(run: RunStatus) -> dict[str, str]:
    """요청의 자격 증명 참조가 모두 secret:// 이고 암호화 저장소에 실제로 있을 때만 보고합니다.

    저장소를 확인할 수 없으면 모르는 값이므로 보고하지 않습니다 (화면: 미제공).
    """
    req = run.request
    refs = [r for r in (req.aws_credentials_ref, req.db_admin_ref, req.db_credentials_ref) if r]
    if not refs or not all(r.startswith("secret://") for r in refs):
        return {}
    try:
        from app.core.secrets import has_secret

        if all(has_secret(r) for r in refs):
            return {"secret_encryption": "ENCRYPTED"}
    except (ImportError, OSError, sqlite3.Error) as exc:
        log.warning("시크릿 저장소를 확인하지 못해 암호화 지표를 생략합니다 (%s)", type(exc).__name__)
    return {}


def _private_ip_metric(output: Any) -> dict[str, str]:
    """provisioning 이 돌려준 Cloud SQL 주소를 직접 확인합니다. 주소가 없으면 보고하지 않습니다."""
    target = output.get("target") if isinstance(output, Mapping) else getattr(output, "target", None)
    db = target.get("db") if isinstance(target, Mapping) else None
    ip = db.get("private_ip") if isinstance(db, Mapping) else None
    try:
        is_private = ipaddress.ip_address(ip).is_private
    except (TypeError, ValueError):
        return {}
    return {"cloud_sql_private_ip": "SECURE" if is_private else "PUBLIC"}


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # float 로 바꿀 수 없을 만큼 큰 정수
        return False


def _cutover_evidence(evidence: Any) -> Mapping[str, Any]:
    section = evidence.get("cutover") if isinstance(evidence, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _worker_cutover_seconds(evidence: Any) -> float | None:
    """팀 합의(박준서, 11/5): released_at - hold_started_at. 에이전트가 t0·t1 로 저장합니다."""
    section = _cutover_evidence(evidence)
    t0, t1 = _aware_datetime(section.get("t0")), _aware_datetime(section.get("t1"))
    if t0 is None or t1 is None or t1 < t0:
        return None
    return round((t1 - t0).total_seconds(), 3)


_MASK = "[숨김]"
_QUOTED = r"""(?:"(?:[^"\\]|\\.)*(?:"|$)|'(?:[^'\\]|\\.)*(?:'|$))"""
_VALUE = rf"(?:{_QUOTED}|\S+)"
_KEY_START = r"(?<![\w-])[\w-]*"
_INFRA_HOST = r"(?<![a-z0-9.-])(?:[a-z0-9-]+\.)+(?:amazonaws\.com|run\.app|workers\.dev|internal|local)\b"
_HOST_PORT = r"(?<![a-z0-9.-])(?:(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*|localhost):\d+\b"
_IPV4 = r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"
_BRACKETED_IPV6 = re.compile(r"\[([0-9a-f:.]*:[0-9a-f:.]*(?:%[\w.-]+)?)\](?::\d+)?", re.IGNORECASE)
_BARE_IPV6 = re.compile(r"(?<![\w:.])[0-9a-f:.]*:[0-9a-f:.]*(?:%[\w.-]+)?", re.IGNORECASE)
# 순서가 중요합니다. 뒤를 통째로 지우는 규칙과 URL 을 먼저 처리해야 그 안의 값이 다른 규칙에 조각나지 않습니다.
_SCRUB_BEFORE_IPV6 = tuple(
    (re.compile(pattern, re.IGNORECASE | re.DOTALL), replacement)
    for pattern, replacement in (
        (rf"""{_KEY_START}(?:sslpassword|token\s*=|token["']\s*:).*""", _MASK),
        (r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://\S+", _MASK),
        (r"(?<!\S)\S+@\S+", _MASK),
        (rf"""({_KEY_START}(?:password|passwd|pwd))["']?\s*[=:]\s*{_VALUE}""", rf"\1={_MASK}"),
        (rf"({_KEY_START}(?:hostname|hostaddr|host|username|user|dbname))\s*=\s*{_VALUE}", rf"\1={_MASK}"),
        (r",?\s*\bport\s*=?\s*\d+", ""),
        (rf"\bserver at\s+{_VALUE}", f"server at {_MASK}"),
        (rf"\bserver on socket\s+{_VALUE}", f"server on socket {_MASK}"),
        (rf"\bhost(?:\s*name)?\s+{_QUOTED}", f"host {_MASK}"),
        (rf"\b(user|role)\s+{_QUOTED}", rf"\1 {_MASK}"),
    )
)
_SCRUB_AFTER_IPV6 = tuple(
    (re.compile(pattern, re.IGNORECASE), _MASK) for pattern in (_IPV4, _HOST_PORT, _INFRA_HOST)
)


def _is_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def _mask_bracketed_ipv6(match: re.Match[str]) -> str:
    return _MASK if _is_ipv6(match[1]) else match[0]


def _mask_bare_ipv6(match: re.Match[str]) -> str:
    """시각(12:30:45)과 문장 부호를 주소로 보지 않도록 실제로 IPv6 인지 확인합니다."""
    token = match[0]
    if _is_ipv6(token):
        return _MASK
    stripped = token.rstrip(":.")
    return _MASK + token[len(stripped):] if _is_ipv6(stripped) else token


def _scrub_agent_text(value: Any) -> str | None:
    """에이전트 오류 문구에서 접속 정보를 지웁니다. 화면·저장 지표·AI 진단으로 나가기 전에 거칩니다.

    문자열이 아니면 None 입니다. 부르는 쪽이 '상세 불명' 등으로 대체합니다.
    """
    if not isinstance(value, str):
        return None
    text = value[:_MAX_AGENT_TEXT_SCANNED]
    for pattern, replacement in _SCRUB_BEFORE_IPV6:
        text = pattern.sub(replacement, text)
    text = _BRACKETED_IPV6.sub(_mask_bracketed_ipv6, text)
    text = _BARE_IPV6.sub(_mask_bare_ipv6, text)
    for pattern, replacement in _SCRUB_AFTER_IPV6:
        text = pattern.sub(replacement, text)
    return text[:_MAX_AGENT_TEXT]


def scrub_sensitive_text(value: Any, fallback: str = "상세 불명") -> str:
    """오류 문구에서 URL, 패스워드, 토큰, IP 등 민감 접속 정보를 지웁니다."""
    scrubbed = _scrub_agent_text(value)
    return scrubbed if scrubbed else fallback


def _without_raw_errors(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """measure_error 는 예외 원문이고 화면이 쓰지 않으므로 넘기지 않습니다. 전환 전 점검은 에이전트에서 직접 읽습니다."""
    return {key: value for key, value in metrics.items() if key != "measure_error"}


def _sanitize_step(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item.get("step"), str):
        return None
    step: dict[str, Any] = {"step": item["step"]}
    for key in _STEP_NUMBER_KEYS:
        if _is_number(item.get(key)):
            step[key] = item[key]
    error = _scrub_agent_text(item.get("error"))
    if error is not None:
        step["error"] = error
    return step


def _cutover_steps(evidence: Any) -> list[dict[str, Any]] | None:
    """evidence.cutover.steps 를 허용된 키만 남겨 돌려줍니다. 형식이 틀리면 None (화면: 미제공)."""
    steps = _cutover_evidence(evidence).get("steps")
    if not isinstance(steps, list) or not all(isinstance(item, Mapping) for item in steps):
        return None
    sanitized = (_sanitize_step(item) for item in steps)
    return [step for step in sanitized if step is not None]


def _verify_cutover_sequence(evidence: Any) -> dict[str, Any]:
    """CUTOVER_SEQUENCE 가 그 순서대로 있었는지 봅니다. 사이에 다른 단계가 끼어도 됩니다."""
    steps = _cutover_steps(evidence)
    if steps is None:
        return {}
    names = [step["step"] for step in steps]
    position = 0
    missing = []
    for expected in CUTOVER_SEQUENCE:
        accepted = _CUTOVER_STEP_ALIASES.get(expected, (expected,))
        found = next((i for i in range(position, len(names)) if names[i] in accepted), None)
        if found is None:
            missing.append(expected)
        else:
            position = found + 1
    if missing:
        return {"cutover_sequence": "INCOMPLETE", "cutover_sequence_missing": missing}
    return {"cutover_sequence": "VERIFIED"}


def _cutover_metrics(metrics: Any, evidence: Any = None) -> dict[str, Any]:
    """에이전트 보고를 화면 키로 옮깁니다. 보고 없는 값은 만들지 않습니다."""
    result = _without_raw_errors(metrics) if isinstance(metrics, Mapping) else {}
    if result.get("traffic_mode") == "TARGET":
        result["serving"] = "gcp"
    worker_seconds = _worker_cutover_seconds(evidence)
    cutover_ms = result.get("cutover_ms")
    if worker_seconds is not None:
        result.update(cutover_seconds=worker_seconds, cutover_seconds_source="worker")
    elif _is_number(cutover_ms):
        result.update(cutover_seconds=round(cutover_ms / 1000, 3), cutover_seconds_source="agent")
    steps = _cutover_steps(evidence)
    if steps is not None:
        result["cutover_steps"] = steps
    return result


def _best_effort(read: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    """증적을 못 읽어도 전환 결과 판정은 막지 않습니다. 못 읽은 값은 미제공으로 둡니다."""
    try:
        return read(*args)
    except Exception as exc:
        log.warning("컷오버 증적을 읽지 못했습니다 (%s)", type(exc).__name__)
        return {}


def _storable(job: Mapping[str, Any]) -> dict[str, Any]:
    """짝 없는 서로게이트처럼 저장할 수 없는 문자를 바꿉니다. 저장 실패가 끝난 전환을 실패로 만들지 않게 합니다."""
    text = json.dumps(job, ensure_ascii=False, default=str)
    return json.loads(text.encode("utf-8", "replace").decode("utf-8"))


def _read_job(agent: Any, job_id: str) -> dict[str, Any]:
    """한 번의 조회 실패로 전환 결과를 판정하지 않습니다. 못 읽으면 빈 값이고, 다음 폴링에서 다시 봅니다."""
    try:
        job = agent.get_job(job_id)
        if not isinstance(job, Mapping):
            raise TypeError(type(job).__name__)
        return _storable(job)
    except Exception as exc:
        log.warning("컷오버 상태 조회 실패, 다시 조회합니다 (%s)", type(exc).__name__)
        return {}


def _outcome_if_not_started(run: RunStatus) -> str:
    """이번 시도가 시작되지 못했을 때의 결과입니다. 앞선 시도에서 트래픽이 넘어갔을 수 있으면 그 판정을 지우지 않습니다."""
    prior = run.stage(StageName.CUTOVER).metrics.get(CUTOVER_OUTCOME)
    return "not_started" if prior is None or prior in CUTOVER_ON_SOURCE else prior


def _read_worker_status() -> Mapping[str, Any] | None:
    """Worker 의 지금 상태입니다. 설정이 없거나 읽지 못하면 None 입니다."""
    try:
        worker = _cutover_worker()
        status = worker.status() if worker is not None else None
    except Exception as exc:
        log.warning("Worker 상태를 읽지 못했습니다 (%s)", type(exc).__name__)
        return None
    return status if isinstance(status, Mapping) else None


def _worker_confirms_source() -> bool:
    """트래픽이 원본에 있다고 Worker 가 직접 말할 때만 True 입니다."""
    status = _read_worker_status()
    return status is not None and status.get("mode") == "SOURCE" and status.get("next_origin") == "SOURCE"


def _rollback_outcome(job: Mapping[str, Any]) -> str:
    """에이전트가 롤백을 보고해도 복귀 단계가 실패했거나 Worker 가 원본을 확인하지 않으면 unknown 입니다.

    경로 변경 응답만 잃으면 에이전트는 경로가 바뀐 줄 모른 채 cutover_ready 를 보고합니다. 이때 안전하다고
    표시하면 운영자가 중단해 복제를 지울 수 있습니다.
    """
    try:
        steps = _cutover_steps(job.get("evidence")) or []
        if any(step["step"].endswith("_failed") for step in steps):
            return "unknown"
    except Exception as exc:
        log.warning("복귀 단계를 확인하지 못했습니다 (%s)", type(exc).__name__)
        return "unknown"
    return "rolled_back" if _worker_confirms_source() else "unknown"


def _post_cutover(
    agent: Any, ctx: RunContext, job_id: str, approved_by: str, not_started: str
) -> tuple[str, str | None]:
    """전환을 요청합니다. 결과는 accepted(접수), refused(409 거절), lost(응답을 잃어 닿았는지 모름) 중 하나와 거절 사유입니다."""
    from app.features.db_migration.client import AgentError

    try:
        agent.cutover(job_id, approved_by=approved_by)
    except AgentError as exc:
        if exc.status_code == 409:
            return "refused", _scrub_agent_text(exc.detail)
        if exc.status_code < 500:
            ctx.report("running", 10, {CUTOVER_OUTCOME: not_started})
            raise RuntimeError(f"에이전트가 전환 요청을 거부했습니다 (HTTP {exc.status_code})") from None
        log.warning("컷오버 요청 응답 오류, 상태로 판정합니다 (HTTP %s)", exc.status_code)
        return "lost", None
    except Exception as exc:  # 응답만 잃었을 수 있으므로 실패로 치지 않고 상태를 봅니다.
        log.warning("컷오버 요청 응답을 받지 못했습니다, 상태로 판정합니다 (%s)", type(exc).__name__)
        return "lost", None
    return "accepted", None


def _job_progress(job: Mapping[str, Any]) -> int:
    """잘못된 progress 때문에 report 가 실패해 롤백 메시지를 가리지 않게 합니다."""
    progress = job.get("progress")
    return int(min(100, max(0, progress))) if _is_number(progress) else _DEFAULT_CUTOVER_PROGRESS


def _cutover_approver(run: RunStatus) -> str:
    """가장 최근 승인 ② 의 승인자입니다. 이름이 없으면 이전 승인자로 돌아가지 않습니다 (감사 추적용)."""
    record = next((r for r in reversed(run.approval_records) if r.gate == "cutover"), None)
    return (record.approver if record else None) or "workflows"


def _agent_is_simulated() -> bool:
    from app.features.db_migration.client import AgentSettings

    try:
        return AgentSettings.from_env().is_simulation
    except KeyError:
        return False


DISCOVERY_MERGED_MESSAGE = "assessment 에 통합 — 원본 조회는 planning 단계에서 합니다"


def _extract_plan_target_infra(run: RunStatus) -> dict[str, Any]:
    planning_out = run.outputs.get(StageName.PLANNING.value)
    if not planning_out:
        raise MissingStageOutput("planning 단계 출력이 없습니다")
    plan = planning_out.get("plan")
    if not plan:
        raise MissingStageOutput("planning 단계의 plan 출력이 없습니다")
    target_infra = (
        plan.get("target_infra")
        if isinstance(plan, dict)
        else getattr(plan, "target_infra", None)
    )
    if not target_infra:
        raise MissingStageOutput("plan 에 target_infra 가 없습니다")
    if hasattr(target_infra, "model_dump"):
        target_infra = target_infra.model_dump()
    return dict(target_infra)


def _extract_source_credentials_ref(run: RunStatus) -> str | None:
    """조사 출력의 원본 AWS 키 이름표(source.credentials_ref.cloud). 없으면 None.

    app_deploy 가 원본 이미지(ECR)를 목적지 저장소로 복사할 때 원본을 읽는 데 씁니다.
    """
    planning_out = run.outputs.get(StageName.PLANNING.value) or {}
    source = planning_out.get("source")
    if hasattr(source, "model_dump"):
        source = source.model_dump()
    credentials = source.get("credentials_ref") if isinstance(source, dict) else None
    if hasattr(credentials, "model_dump"):
        credentials = credentials.model_dump()
    ref = credentials.get("cloud") if isinstance(credentials, dict) else None
    return ref if isinstance(ref, str) and ref else None


def run_discovery(run: RunStatus, ctx: RunContext) -> dict[str, Any]:
    # 11/4 합의로 discovery·planning 은 assessment 하나로 합쳤습니다 (PR #27). 원본 조회 결과는
    # planning 출력의 `source` 에 있으므로 이 단계는 조회 없이 그 사실만 남기고 넘깁니다.
    ctx.report("running", 100, None, DISCOVERY_MERGED_MESSAGE)
    return {"merged_into": StageName.PLANNING.value, "note": DISCOVERY_MERGED_MESSAGE}


def run_assessment(run: RunStatus, ctx: RunContext) -> Any:
    # assessment 가 discovery·planning 을 합친 모듈입니다 (PR #27). 출력은 {source, plan, gaps}.
    req = run.request
    manual = req.source_kind == "manual"
    if not manual and not req.aws_credentials_ref:
        raise MissingStageOutput("원본 AWS 키 이름표가 없습니다")

    try:
        from app.features.assessment import service as assessment_service
        from app.features.assessment.schemas import AssessmentInput
    except ImportError as exc:
        raise RuntimeError("assessment 모듈을 찾을 수 없습니다") from exc

    if manual:
        # 직접 입력한 원본은 AWS 를 조회하지 않습니다. 원본에서 확인하지 않은 값이라는 표시는 assessment 가 gaps 에 남깁니다.
        inp = AssessmentInput(
            run_id=run.run_id,
            source_kind="manual",
            manual=req.manual,
            github_repo_url=req.github_repo_url,
            target_region=req.target_region,
        )
    else:
        inp = AssessmentInput(
            run_id=run.run_id,
            github_repo_url=req.github_repo_url,
            aws_credentials_ref=req.aws_credentials_ref,
            db_admin_ref=req.db_admin_ref or req.db_credentials_ref,
            aws_region=req.aws_region,
            target_region=req.target_region,
            ecs_service=req.ecs_service,
            rds_instance_id=req.rds_instance_id,
        )
    res = assessment_service.run(inp, ctx)
    # IAM 감사를 하는 모듈이 아직 없어 iam_audit_status 는 보고하지 않습니다 (화면: 미제공).
    metrics = _secret_encryption_metric(run)
    if ctx is not None and metrics:
        ctx.report("running", 100, metrics)
    return res


def _check_cdc_settings() -> None:
    """목적지를 만들기 전에 CDC 설정을 봅니다. 빠진 값은 목적지를 다 만든 뒤 db_migration 단계에서야 드러납니다."""
    from app.features.db_migration.client import AgentSettings
    from app.features.db_migration.vpn import OciSettings

    try:
        if AgentSettings.from_env().is_simulation:
            return  # 모의 에이전트로는 실제 VPN 을 만들지 않습니다.
    except KeyError:
        raise RuntimeError("CDC_AGENT_URL 환경변수가 설정되지 않아 CDC 를 시작할 수 없습니다") from None
    OciSettings.from_env()


ALLOW_SAME_ACCOUNT_ENV = "ALLOW_SAME_ACCOUNT_TARGET"


def _extract_source_account_id(run: RunStatus) -> str | None:
    """조사 출력의 원본 AWS 계정 ID(source.account_id). 없으면 None."""
    planning_out = run.outputs.get(StageName.PLANNING.value) or {}
    source = planning_out.get("source")
    if hasattr(source, "model_dump"):
        source = source.model_dump()
    account = source.get("account_id") if isinstance(source, dict) else None
    return str(account) if account else None


def _check_target_account(run: RunStatus) -> None:
    """AWS 목적지 계정이 원본 계정과 같으면 Terraform 을 시작하기 전에 거부합니다.

    같은 계정에 목적지를 만들면 이름(ECR 저장소, IAM 역할 등)이 원본과 겹쳐 실패하거나 원본 계정에 자원이 섞입니다.
    Terraform 의 allowed_account_ids 는 '입력한 값'과 '실행 환경 로그인'만 비교하므로 원본 계정은 여기서 비교합니다.
    계정이 하나뿐인 시연 환경에서는 환경변수 ALLOW_SAME_ACCOUNT_TARGET=1 로 명시해야 통과합니다.
    원본 계정을 알 수 없으면(직접 입력 등) 비교하지 않습니다.
    """
    if provider_of(run.request) != "aws":
        return
    config = run.request.target_infra
    target = (config.project_id if config else None) or os.getenv("AWS_ACCOUNT_ID")
    source = _extract_source_account_id(run)
    if not target or not source or str(target).strip() != source:
        return
    if os.getenv(ALLOW_SAME_ACCOUNT_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return
    raise RuntimeError(
        f"목적지 AWS 계정({target})이 원본 계정과 같습니다. 같은 계정에 만들면 이름이 겹치거나 원본 계정에 자원이 섞일 수 있어 "
        f"시작하지 않았습니다. 다른 계정 ID 를 지정하세요. 같은 계정에 만들려면 환경변수 {ALLOW_SAME_ACCOUNT_ENV}=1 을 설정하세요"
    )


def run_provisioning(run: RunStatus, ctx: RunContext) -> Any:
    # planning 출력의 target_infra 로 목적지 인프라(VPC, Cloud SQL)를 프로비저닝합니다 (PR #40).
    target_infra = _extract_plan_target_infra(run)
    _check_target_account(run)
    _check_cdc_settings()
    res = adapter_for(run.request).provision(run.run_id, target_infra, run.request.target_infra, ctx)
    metrics = _private_ip_metric(res)
    if ctx is not None and metrics:
        ctx.report("running", 100, metrics)
    return res


def clean_up_cdc(run: RunStatus) -> bool:
    """중단된 run 의 복제 자원(커넥터, 복제 슬롯, VPN)을 service.stop 으로 정리합니다. 정리할 것이 없으면 False 입니다.

    CDC 추적이 이미 끝났거나 서버가 재시작되면 run 을 중단해도 정리할 주체가 없어 슬롯이 남고 원본에 WAL 이 쌓입니다.
    """
    stage = run.stage(StageName.DB_MIGRATION)
    if stage.manual or stage.state == WorkflowState.PENDING:
        return False

    from app.features.db_migration import service as db_migration_service
    from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient
    from app.features.db_migration.guide_workflow import to_job_id

    try:
        settings = AgentSettings.from_env()
    except KeyError:
        log.warning("CDC_AGENT_URL 이 없어 run %s 의 복제 자원을 정리하지 않습니다", run.run_id)
        return False

    agent = CdcAgentClient(settings)
    try:
        job = agent.get_job(to_job_id(run.run_id))
    except AgentError as exc:
        if exc.status_code != 404:
            raise
        job = None  # 작업 등록 전이어도 VPN 은 만들어졌을 수 있습니다.
    # 에이전트는 전환 이후의 정리를 거부합니다. 그 상태로 stop 하면 VPN 만 지워져 원본 슬롯을 지울 길이 끊깁니다.
    if job is not None and job.get("phase") in _PAST_CUTOVER_PHASES:
        raise RuntimeError("전환을 시작한 작업은 자동으로 정리하지 않습니다. 트래픽과 데이터를 확인한 뒤 처리하세요")

    # 에이전트 단계만 믿지 않습니다. 트래픽이 목적지에 있으면 복제를 지우는 순간 되돌릴 길이 없어집니다.
    outcome = run.stage(StageName.CUTOVER).metrics.get(CUTOVER_OUTCOME)
    if outcome is not None and outcome not in CUTOVER_ON_SOURCE and not _worker_confirms_source():
        raise RuntimeError("트래픽이 목적지로 넘어갔을 수 있어 자동으로 정리하지 않습니다. Worker 상태와 데이터를 확인한 뒤 처리하세요")

    db_migration_service.stop(run.run_id, agent=agent)
    return True


def decommission_cdc(run: RunStatus) -> str:
    """전환이 끝난 run 의 이전 자원을 db_workflow.finish 로 정리합니다 (에이전트 작업 종료 → 복제 슬롯 정리 → VPN 삭제).

    남겨 두면 VPN 연결과 CDC 서버가 매달 청구되고, 원본 복제 슬롯이 WAL 을 계속 붙잡습니다.
    끝나면 사람에게 보일 문장을, 남은 항목이 있으면 그 이름과 함께 예외를 냅니다 (다시 부를 수 있음).
    전환 단계·작업 단계 확인(stabilizing 또는 closed)은 db_workflow.finish 가 합니다.
    """
    stage = run.stage(StageName.DB_MIGRATION)
    if stage.manual or stage.state == WorkflowState.PENDING:
        return "DB 이전을 수동으로 기록한 run 이라 자동으로 정리할 복제 자원이 없습니다. 직접 만든 자원은 직접 지우세요"

    from app.features.db_migration import db_workflow
    from app.features.db_migration.client import AgentSettings, CdcAgentClient

    try:
        settings = AgentSettings.from_env()
    except KeyError:
        raise RuntimeError("CDC_AGENT_URL 이 없어 이전 자원을 정리하지 못했습니다") from None
    result = db_workflow.finish(run.run_id, agent=CdcAgentClient(settings))
    if result.state != "finished":
        left = ", ".join(result.remaining) or "확인 필요"
        detail = f" ({result.message})" if result.message else ""
        raise RuntimeError(f"남은 항목: {left}{detail}. 다시 시도하세요")
    vpn = "VPN 삭제" if result.vpn == "removed" else "VPN 남음"
    return " ".join([f"이전 자원 정리 완료: 복제 작업 종료, {vpn}.", *result.next_steps])


def _cdc_end_reason(result: Any) -> str | None:
    """db_migration 이 끝난 이유. 전환이 끝나 돌아온 것만 정상 종료이고, 그 밖의 streaming 이후 종료에는 항상 사유를 만듭니다."""
    is_mapping = isinstance(result, Mapping)
    state = result.get("state") if is_mapping else getattr(result, "state", None)
    if state in _SWITCHED_PHASES:
        return None
    message = result.get("message") if is_mapping else getattr(result, "message", None)
    return message or f"CDC 복제 작업이 끝났습니다 (상태: {state})"


def _report_cdc_ended(ctx: RunContext, reason: str) -> None:
    reason = _scrub_agent_text(reason) or "상세 불명"
    try:
        ctx.report("running", 100, {"cdc_error": reason}, f"CDC 복제 중단: {reason}")
    except RunAborted:
        pass  # run 이 이미 중단됐으면 남길 것이 없습니다.
    except Exception:
        log.exception("CDC 종료를 기록하지 못했습니다")


def run_db_migration(
    run: RunStatus,
    ctx: RunContext,
    *,
    poll_interval_s: float = 0.5,
    timeout_s: float = CDC_READY_TIMEOUT_S,
) -> Any:
    # assessment(source)와 provisioning(target) 결과를 조립하여 CDC DB 마이그레이션을 실행합니다.
    planning_out = run.outputs.get(StageName.PLANNING.value)
    if not planning_out or not planning_out.get("source"):
        raise MissingStageOutput("planning 단계의 원본(source) 출력이 없습니다")
    source = planning_out["source"]
    if hasattr(source, "model_dump"):
        source = source.model_dump()

    provisioning_out = run.outputs.get(StageName.PROVISIONING.value)
    if not provisioning_out or not provisioning_out.get("target"):
        raise MissingStageOutput("provisioning 단계의 대상(target) 출력이 없습니다")
    target = provisioning_out["target"]
    if hasattr(target, "model_dump"):
        target = target.model_dump()

    from app.features.db_migration import service as db_migration_service
    from app.features.db_migration.guide_workflow import APP_DEPLOY_PHASES, to_job_id

    source_dict = dict(source)
    if "tables" in source_dict and isinstance(source_dict["tables"], list):
        # alpha_cdc.cutover_marker 등 내부 CDC 마커 테이블은 에이전트가 자동 관리하므로 제외합니다 (LyleKim 피드백 반영).
        source_dict["tables"] = [
            t for t in source_dict["tables"]
            if not (isinstance(t, str) and (t.startswith("alpha_cdc.") or t == "alpha_cdc.cutover_marker"))
        ]

    inp = {
        "run_id": run.run_id,
        "source": source_dict,
        "target": dict(target),
    }

    if _agent_is_simulated():
        # 모의 에이전트로 실제 VPN 을 만들지 않습니다. 화면에는 simulated 지표로 구분됩니다.
        inp["options"] = {"skip_vpn": True}
        if ctx is not None:
            ctx.report("running", 0, {"simulated": True}, "시뮬레이션 모드: 실제 VPN·CDC 없이 진행합니다")

    job_id = to_job_id(run.run_id)
    ready_event = threading.Event()
    result_holder: list[Any] = []
    error_holder: list[Exception] = []
    latest_metrics: dict[str, Any] = {}
    latest_phase = ["init"]
    run_aborted = threading.Event()

    class _ReportingProxy:
        def report(
            self,
            state: str,
            progress: int,
            metrics: dict[str, Any] | None = None,
            message: str | None = None,
        ) -> None:
            forwarded = _without_raw_errors(metrics) if metrics else metrics
            if forwarded:
                latest_metrics.update(forwarded)
                phase = forwarded.get("phase")
                if phase:
                    latest_phase[0] = phase
                    if phase in APP_DEPLOY_PHASES:
                        ready_event.set()
                lag = forwarded.get("replication_lag_bytes")
                if lag is not None and isinstance(lag, (int, float)):
                    try:
                        from app.features.provisioning.worker_client import WorkerClient

                        worker = WorkerClient.from_env()
                        if worker is not None:
                            worker.sync_lag(int(lag))
                    except Exception:
                        pass
            if ctx is not None and not run_aborted.is_set():
                # db_migration/service.py 가 종료 시점에 ctx.report("failed", ...) 또는 ctx.report("stopped", ...) 를 호출할 수 있습니다.
                # workflows 규격상 완료/실패는 runner 반환 및 예외로 판정하므로, reportable 상태일 때만 forward 합니다.
                if state in ("running", "waiting_approval", "paused"):
                    try:
                        ctx.report(state, progress, forwarded, message)
                    except RunAborted:
                        # 예외를 올리면 guide_workflow 가 복제와 VPN 을 지워 clean_up_cdc 의 전환 보호를 우회합니다.
                        # 정리는 clean_up_cdc 만 합니다. service.stop 이 작업을 멈추면 이 추적이 그것을 보고 VPN 을 지우므로 계속 돕니다.
                        run_aborted.set()
                        ready_event.set()  # 기다리던 runner 가 바로 중단을 알아챕니다.

    # runner 가 streaming 으로 먼저 반환한 뒤의 종료는 runner 가 처리할 수 없어 지표로 남깁니다.
    handoff_lock = threading.Lock()
    worker_done = [False]
    runner_returned = [False]

    def _worker() -> None:
        ended_with: str | None
        try:
            res = db_migration_service.run(inp, _ReportingProxy())
            result_holder.append(res)
            ended_with = _cdc_end_reason(res)
        except Exception as exc:
            error_holder.append(exc)
            ended_with = str(exc) or type(exc).__name__
        finally:
            ready_event.set()
        with handoff_lock:
            worker_done[0] = True
            is_background = runner_returned[0]
        if is_background and ctx is not None and ended_with is not None:
            _report_cdc_ended(ctx, ended_with)

    thread = threading.Thread(target=_worker, daemon=True, name=f"cdc-runner-{job_id}")
    thread.start()

    start_t = time.monotonic()
    while True:
        if error_holder:
            raise error_holder[0]

        if result_holder:
            res = result_holder[0]
            if hasattr(res, "state") and res.state == "failed":
                raise RuntimeError(getattr(res, "message", "db_migration 실패"))
            if isinstance(res, dict) and res.get("state") == "failed":
                raise RuntimeError(res.get("message", "db_migration 실패"))
            return res

        if ready_event.is_set():
            with handoff_lock:
                if worker_done[0]:
                    continue  # 방금 끝났으면 위의 결과 처리로 돌아갑니다.
                runner_returned[0] = True
            phase = latest_phase[0]
            if ctx is not None:
                ctx.report("running", 100, latest_metrics, f"CDC 스트리밍 복제 중 ({phase})")
            return {
                "job_id": job_id,
                "phase": phase,
                "state": "streaming",
                "metrics": latest_metrics,
            }

        if time.monotonic() - start_t > timeout_s:
            # 그냥 실패로 끝내면 뒤의 작업이 계속 돌아 같은 run 의 재시도가 "already running" 으로 막힙니다.
            # 스트리밍 전이라 전환할 것이 없으므로 멈춤을 요청합니다.
            note = _stop_unready_cdc(db_migration_service, run.run_id)
            raise TimeoutError(f"CDC 스트리밍 진입 대기 시간({timeout_s:.0f}초)을 초과했습니다. {note}")

        time.sleep(poll_interval_s)


def _stop_unready_cdc(service: Any, run_id: str) -> str:
    """스트리밍에 들어가지 못한 db_migration 에 멈춤을 요청하고, 다음에 할 일을 문장으로 돌려줍니다."""
    try:
        service.stop(run_id)
    except Exception as exc:
        log.exception("스트리밍 전 CDC 작업을 멈추지 못했습니다 (run %s)", run_id)
        return f"복제 작업 정리를 요청하지 못했습니다 ({type(exc).__name__}). 정리한 뒤 다시 시도하세요"
    return ("복제 작업에 정리를 요청했습니다. 진행 중인 VPN 단계가 끝나면 정리되고, 그 뒤에 다시 시도할 수 있습니다 "
            "(에이전트에 작업이 이미 등록됐다면 새 run 으로 시작하세요)")


def run_app_deploy(run: RunStatus, ctx: RunContext) -> Any:
    """DB 마이그레이션 스키마 준비 후 목적지 Cloud Run 앱을 배포하고 /health/ready 200 을 검증합니다.

    실행 조건 (환경변수):
      - WORKER_URL: Cloudflare Worker 주소
      - WORKER_TOKEN_FILE: Worker 인증 토큰 파일 경로
      - GCP_PROJECT_ID: 배포 대상 GCP 프로젝트 ID
    """
    target_infra = _extract_plan_target_infra(run)
    return adapter_for(run.request).deploy_app(
        run.run_id,
        target_infra,
        run.request.target_infra,
        ctx,
        source_credentials_ref=_extract_source_credentials_ref(run),
    )


def run_cutover(
    run: RunStatus,
    ctx: RunContext,
    *,
    poll_interval_s: float = 1.0,
    timeout_s: float = 60.0,
    probe_settle_s: float = PROBE_SETTLE_S,
    verification_wait_s: float = VERIFICATION_WAIT_S,
) -> Any:
    """전환을 실행하고 실측값을 남긴 뒤, 전환 시점 데이터 비교가 끝날 때까지 봅니다.

    ALPHA_PROBE_URL 이 있으면 외부 프로버로 읽기 실패 수와 대기 시간을, WORKER_URL 이 있으면 Worker 집계로
    쓰기 실패 수를 잽니다. 시뮬레이션에서는 실제 전환이 없어 잴 대상이 없으므로 둘 다 켜지 않습니다.
    측정은 전환 직후에 끝납니다. 비교는 오래 걸릴 수 있고 전환 시간과 상관이 없습니다.
    """
    is_simulated = _agent_is_simulated()
    prober = None if is_simulated else prober_from_env()
    worker = None if is_simulated else _cutover_worker()
    if prober is None and worker is None:
        job = _request_cutover(run, ctx, poll_interval_s, timeout_s)
    else:
        if prober is not None:
            prober.start()
        measured = ProbedContext(ctx, prober)
        try:
            job = _request_cutover(run, measured, poll_interval_s, timeout_s)
            time.sleep(probe_settle_s)  # 전환 직후 요청까지 측정에 포함합니다.
        finally:
            _finish_measurement(measured, worker, run)
    return _await_verification(run, ctx, job, poll_interval_s, verification_wait_s)


def _verification(job: Mapping[str, Any]) -> Any:
    metrics = job.get("metrics")
    return metrics.get("verification") if isinstance(metrics, Mapping) else None


def _switched_message(job: Mapping[str, Any]) -> str:
    msg = _scrub_agent_text(job.get("message"))
    message = f"트래픽 전환 완료 ({msg})" if msg else "트래픽 전환 완료 (안정화 관찰 중)"
    missing = _best_effort(_verify_cutover_sequence, job.get("evidence")).get("cutover_sequence_missing")
    if missing:
        # 트래픽은 이미 넘어갔으므로 실패로 두지 않고 경고만 남깁니다.
        message = f"{message} — 경고: 전환 순서 증적 불완전 (누락: {', '.join(missing)})"
    return message


def _await_verification(
    run: RunStatus,
    ctx: RunContext,
    job: Mapping[str, Any],
    poll_interval_s: float,
    wait_s: float,
) -> Any:
    """전환 시점 데이터 비교 결과를 받아 단계 결과에 반영합니다. 보고하지 않는 에이전트는 기다리지 않습니다.

    불일치면 단계를 실패로 남깁니다. 트래픽은 이미 목적지에 있어 되돌리지 않고 사람이 확인합니다.
    끝내지 못한 비교(unverifiable)와 기다리다 못 받은 결과는 데이터가 다르다는 뜻이 아니므로 경고만 남깁니다.
    """
    if _verification(job) is None:
        return job

    from app.features.db_migration.client import AgentSettings, CdcAgentClient

    agent = CdcAgentClient(AgentSettings.from_env())
    job_id = _cutover_job_id(run)
    deadline = time.monotonic() + wait_s
    while _verification(job) == VERIFICATION_RUNNING and job.get("state") != "failed":
        if time.monotonic() > deadline:
            warning = f"경고: 전환 시점 데이터 비교 결과를 {wait_s:g}초 안에 받지 못했습니다. 데이터를 직접 확인하세요"
            ctx.report("running", 100, {"verification": VERIFICATION_UNKNOWN}, f"{_switched_message(job)} — {warning}")
            return job
        time.sleep(poll_interval_s)
        job = _read_job(agent, job_id) or job
        metrics = _best_effort(_cutover_metrics, job.get("metrics"), job.get("evidence"))
        ctx.report("running", _job_progress(job), metrics, _switched_message(job))

    metrics = _best_effort(_cutover_metrics, job.get("metrics"), job.get("evidence"))
    if job.get("state") == "failed":
        ctx.report("running", _job_progress(job), {**metrics, CUTOVER_OUTCOME: "post_switch_failed"})
        raise RuntimeError(f"전환 후 확인 필요: {_scrub_agent_text(job.get('message')) or '상세 불명'}")
    ctx.report("running", 100, metrics, _switched_message(job))
    return job


def _finish_measurement(measured: ProbedContext, worker: Any, run: RunStatus) -> None:
    """측정 마무리에 실패해도 끝난 전환의 결과를 바꾸지 않습니다. 프로버는 finish 에서 항상 멈춥니다."""
    extra = _write_request_metrics(worker, _cutover_job_id(run)) if worker is not None else None
    try:
        measured.finish(extra)
    except RunAborted:
        raise
    except Exception as exc:
        log.warning("전환 측정값을 남기지 못했습니다 (%s)", type(exc).__name__)


def _cutover_job_id(run: RunStatus) -> str:
    from app.features.db_migration.guide_workflow import to_job_id

    return to_job_id(run.run_id)


def _cutover_worker() -> Any:
    """Worker 집계를 읽을 클라이언트. 설정이 없거나 쓸 수 없으면 None 이고, 화면은 '미제공' 입니다."""
    from app.features.provisioning.terraform import ProvisioningError
    from app.features.provisioning.worker_client import WorkerClient

    try:
        return WorkerClient.from_env()
    except (ProvisioningError, OSError) as exc:
        log.warning("Worker 집계를 읽을 수 없어 쓰기 실패 수를 생략합니다 (%s)", type(exc).__name__)
        return None


def _write_request_metrics(worker: Any, job_id: str) -> dict[str, Any]:
    """보류 시작부터 Worker 가 센 쓰기 요청. 이번 보류의 집계라고 확인되지 않으면 보고하지 않습니다."""
    try:
        status = worker.status()
    except Exception as exc:
        log.warning("Worker 상태를 읽지 못해 쓰기 실패 수를 생략합니다 (%s)", type(exc).__name__)
        return {}
    if not isinstance(status, Mapping):
        return {}
    counts = status.get("request_counts")
    hold_started_at = status.get("hold_started_at")
    is_this_hold = (
        isinstance(counts, Mapping)
        and status.get("run_id") == job_id
        and hold_started_at is not None
        and counts.get("since") == hold_started_at
    )
    if not is_this_hold or not (_is_number(counts.get("write_total")) and _is_number(counts.get("write_failed"))):
        return {}
    return {"write_requests": counts["write_total"], "write_failed_requests": counts["write_failed"]}


def _request_cutover(
    run: RunStatus,
    ctx: RunContext,
    poll_interval_s: float,
    timeout_s: float,
) -> Any:
    """승인 ② 완료 후 CDC 에이전트에 cutover 요청(hold -> switch -> release)을 전달하고,
    에이전트가 stabilizing(전환 완료) 또는 failed/waiting_approval(롤백)에 도달할 때까지 대기합니다.
    전환 시간은 여기서 확정합니다. 뒤따르는 데이터 비교는 _await_verification 이 봅니다.

    cutover_outcome 은 항상 지금 말할 수 있는 만큼만 씁니다. 도중에 끊겨도 원본에 있다고 잘못 말하지 않게
    요청을 보내기 전에 unknown 으로 바꾸고, 결과를 본 뒤에만 덮어씁니다.
    """
    from app.features.db_migration.client import AgentSettings, CdcAgentClient
    from app.features.db_migration.guide_workflow import to_job_id

    job_id = to_job_id(run.run_id)
    not_started = _outcome_if_not_started(run)
    ctx.report("running", 10, {CUTOVER_OUTCOME: not_started}, "트래픽 전환(Cutover) 요청 중")

    try:
        settings = AgentSettings.from_env()
        if not settings.url:
            raise KeyError("CDC_AGENT_URL")
    except (KeyError, ValueError):
        raise RuntimeError("CDC_AGENT_URL 환경변수가 설정되지 않아 트래픽 전환을 호출할 수 없습니다") from None

    agent = CdcAgentClient(settings)
    # 에이전트는 시도마다 증적을 새로 만듭니다. 요청 전 값과 같으면 그 증적은 앞선 시도의 것입니다.
    evidence_before = _cutover_evidence(_read_job(agent, job_id).get("evidence"))
    ctx.report("running", 10, {CUTOVER_OUTCOME: "unknown"})
    post, refusal = _post_cutover(agent, ctx, job_id, _cutover_approver(run), not_started)

    start_t = time.monotonic()
    while True:
        job = _read_job(agent, job_id)
        state = job.get("state")
        phase = job.get("phase")
        msg = _scrub_agent_text(job.get("message"))
        metrics = _best_effort(_cutover_metrics, job.get("metrics"), job.get("evidence"))

        # 1. 성공 상태 (에이전트가 stabilizing 또는 succeeded 도달)
        if phase == "stabilizing" or state == "succeeded":
            sequence = _best_effort(_verify_cutover_sequence, job.get("evidence"))
            progress = _job_progress(job) if _verification(job) == VERIFICATION_RUNNING else 100
            ctx.report("running", progress, {**metrics, **sequence, CUTOVER_OUTCOME: "switched"}, _switched_message(job))
            return job

        # 2. 롤백 상태 (전환 실패 후 원본 복귀)
        # 요청 응답을 잃었으면 아직 처리 전일 수 있어 롤백으로 보지 않고 계속 봅니다. 끝까지 모르면 unknown 입니다.
        # 거절됐는데 증적이 그대로면 이번 요청으로 돈 전환이 없으니 롤백이 아닙니다 (Worker 검사 거절 등).
        attempted = post != "refused" or _cutover_evidence(job.get("evidence")) != evidence_before
        if (state == "waiting_approval" or phase == "cutover_ready") and post != "lost" and attempted:
            outcome = _rollback_outcome(job)
            ctx.report("running", _job_progress(job), {**metrics, CUTOVER_OUTCOME: outcome})
            if outcome == "rolled_back":
                raise RuntimeError(f"컷오버 실패로 원본으로 롤백되었습니다: {msg or '상세 불명'}")
            raise RuntimeError(f"에이전트가 원본 복귀를 보고했지만 확인하지 못했습니다: {msg or '상세 불명'}")

        # 3. 거절됐고 전환 단계도 아니면 이번 요청으로 시작된 전환이 없습니다.
        if post == "refused" and job and phase != "cutover":
            ctx.report("running", _job_progress(job), {**metrics, CUTOVER_OUTCOME: not_started})
            detail = "; ".join(text for text in (refusal, msg) if text) or "상세 불명"
            raise RuntimeError(f"에이전트가 전환 요청을 거부했습니다 ({state}/{phase}): {detail}")

        # 4. 영구 실패 상태. 에이전트는 전환 단계에서 원본으로 못 돌아갈 때만 failed 를 남깁니다 (jobs.py).
        if state in ("failed", "aborted"):
            outcome = "post_switch_failed" if state == "failed" and phase == "cutover" else "unknown"
            ctx.report("running", _job_progress(job), {**metrics, CUTOVER_OUTCOME: outcome})
            raise RuntimeError(f"컷오버 실패 ({state}): {msg or '상세 불명'}")

        # 5. 진행 중 보고 및 타임아웃 검사
        if time.monotonic() - start_t > timeout_s:
            raise TimeoutError(f"트래픽 전환 완료 대기 시간({timeout_s}초)을 초과했습니다")

        status = msg or ("트래픽 전환 진행 중..." if job else "에이전트 응답 대기 중...")
        ctx.report("running", _job_progress(job), metrics, status)
        time.sleep(poll_interval_s)


RUNNERS: Mapping[StageName, StageRunner] = {
    StageName.DISCOVERY: run_discovery,
    StageName.PLANNING: run_assessment,
    StageName.PROVISIONING: run_provisioning,
    StageName.DB_MIGRATION: run_db_migration,
    StageName.APP_DEPLOY: run_app_deploy,
    StageName.CUTOVER: run_cutover,
}
