"""DB 이전 모듈의 입구이자 전체 절차. 호출하는 쪽은 이 파일의 다섯 함수만 알면 된다.

    run(입력)                 run_id를 바로 돌려주고, 아래를 백그라운드로 진행한다.
      1. 입력 검사, 비밀값 저장     입력 비밀값은 이 시점 이후 이름표로만 다룬다.
      2. 에이전트 확인             Kafka, Connect, 플러그인
      3. 원본 확인                source를 받았으면 그대로, 없으면 넘겨받은 조회 함수로
      4. 시작 전 검사              계정 일치, 공인 접근, 대역 겹침, 동시 실행 (preflight.py)
      5. VPN 확인                 있으면 재사용, 없으면 구축 (vpn.py)
      6. 접속 확인, 비밀 전달, 작업 등록
      7. 초기 복제 → 실시간 복제    에이전트가 진행하고, cutover_ready에서 호출을 기다린다.
    status(run_id)            단계, 진행률, 지표, 정리가 남은 항목
    cutover(run_id, 승인자)    전환 후 결과 반환: 성공 / 원본 복귀 / 수동 확인 / 시간 초과
    finish(run_id)            전환 후 정리: 복제 자원 → VPN → 비밀값
    stop(run_id)              전환 전 중단: 복제 자원 → VPN → 비밀값

정리에 실패하면 VPN과 키를 남긴다. VPN 없이는 원본 복제 슬롯을 지울 수 없고, VPN을 지우려면 키가 필요하다.
남은 항목은 결과의 remaining에 나오며 같은 함수를 다시 부르면 이어서 정리한다.
정리는 run마다 한 번에 하나만 돈다. 도는 동안 다른 정리와 run 호출은 NotAllowed로 거절한다.

TODO(이번 범위 밖): 앱 재시작 후 자동 이어 가기, 실패 자동 복구, 사용자 승인 절차와 감사 기록.
재시작 후에는 같은 입력과 run_id로 run을 다시 부른다. approved_by는 기록만 하고 검증하지 않는다.

execute, stop_job, complete는 workflows가 service.py를 거쳐 쓰는 기존 호출이다.
"""

import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.core.secrets import delete_secret, has_secret, put_secret, resolve

from . import preflight, redact, vpn
from .client import AgentError, AgentSettings, CdcAgentClient
from .input_secrets import INPUT_SECRETS
from .progress_log import ProgressLog
from .schemas import (
    CutoverResult,
    DbMigrationInput,
    DbMigrationResult,
    FinishResult,
    JobOptions,
    JobRequest,
    MigrationOptions,
    RunStatus,
    SourceInput,
    SourceLookup,
    SourceSpec,
    StartInput,
    TargetInput,
    TargetSpec,
)
from .settings import MissingSetting, tunables

logger = logging.getLogger(__name__)

READY_PHASE = "cutover_ready"
CUTOVER_PHASE = "cutover"
STABILIZING_PHASE = "stabilizing"
CLOSED_PHASE = "closed"
METRIC_PHASES = {"streaming", READY_PHASE}
# 복사가 빨리 끝나면 streaming 없이 READY_PHASE로 간다.
APP_DEPLOY_PHASES = {"streaming", READY_PHASE}
TERMINAL_STATES = {"succeeded", "failed", "aborted"}
ENDED_STATES = {"succeeded", "aborted"}
SWITCHED_PHASES = {STABILIZING_PHASE, CLOSED_PHASE}
# 이 상태에서는 정리할 것이 남았는지가 호출자에게 필요하다.
CLEANUP_STATES = {"failed", "stopped", "aborted", "succeeded", "unknown"}
# 마커 테이블은 에이전트가 관리한다. 복제 대상에 넣지 않는다.
INTERNAL_SCHEMA_PREFIX = "alpha_cdc."

# 이 모듈이 만든다. assessment·provisioning의 입력은 플랫폼 소유다.
OWN_SECRETS = ("source-repl", "vpn-psk", "vpn-context")
# vpn-context에는 이름표만 있다.
SECRET_TAGS = (*INPUT_SECRETS, "source-repl", "vpn-psk")
VPN_CONTEXT = "vpn-context"
IAM_NEXT_STEP = "AWS 콘솔에서 이번 이전용으로 만든 IAM 사용자를 삭제하세요. 키는 이 앱에서 이미 지웠습니다."
TARGET_ACCOUNTS_NEXT_STEP = "목적지 계정 {accounts}의 로그인을 막지 못했습니다. 관리자 계정으로 ALTER ROLE ... NOLOGIN을 직접 실행하세요."
OTHER_REFS_NEXT_STEP = "다른 이름표로 저장된 목적지 비밀값은 다른 작업이 쓸 수 있어 지우지 않았습니다. 쓰지 않으면 직접 지우세요: {refs}"
UNVERIFIED_NEXT_STEP = "전환 시점 데이터 비교를 끝내지 못했습니다. 원본과 목적지의 업무 데이터를 직접 확인하세요."
BUSY_MESSAGE = "run {run_id} is already running or being cleaned up"
# 이 run 밖에 저장된 입력의 이름표. 값은 담지 않는다.
OTHER_REFS = "other-refs"

Discover = Callable[[SourceLookup], Any]


class InvalidInput(ValueError):
    """필드 이름만 담는다. pydantic 원문은 입력값을 인용할 수 있다."""

    def __init__(self, fields: list[str]):
        super().__init__(f"invalid input: {', '.join(fields)}")
        self.fields = fields


class NotAllowed(RuntimeError):
    """지금 상태에서는 할 수 없는 호출."""


class UnknownRun(LookupError):
    def __init__(self, run_id: str):
        super().__init__(f"run {run_id} not found")


class _StopBeforeJob(Exception):
    """작업 등록 전에 stop이 왔다."""


class Reporter(Protocol):
    def report(
        self,
        state: str,
        progress: int,
        metrics: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None: ...


@dataclass
class _Progress:
    """작업이 에이전트에 등록되기 전의 진행 상황과, 백그라운드 실행의 결과."""

    stage: str
    state: str = "running"
    message: str | None = None
    source: tuple[str, str] | None = None
    job_registered: bool = False


_active: set[str] = set()
# 실행 중인 run의 stop. 작업 등록 전에는 다른 경로로 run에 닿을 수 없다.
_stop_requested: set[str] = set()
_runs: dict[str, _Progress] = {}
_cutovers: dict[str, Future] = {}
# 정리 중인 run. 같은 Terraform 폴더와 키를 쓰므로 겹치면 안 된다.
_cleaning: set[str] = set()
_active_lock = threading.Lock()


# ---- 공개 함수 ----


def run(inp: StartInput | dict, *, discover: Discover | None = None, agent: CdcAgentClient | None = None,
        ctx: Reporter | None = None) -> str:
    """입력을 검사·저장하고 run_id를 바로 돌려준다. 이후 실패는 status에 나온다."""
    try:
        start = StartInput.model_validate(inp)
    except ValidationError as exc:
        raise InvalidInput(_input_problems(exc)) from None
    if start.source is None and discover is None:
        raise InvalidInput(["source"])
    if start.source is not None and start.source.region != start.region:
        raise InvalidInput(["source.region"])

    if start.run_id:
        _refuse_ended_run(start.run_id, agent)
    run_id = start.run_id or _new_run_id()
    if not _reserve(run_id):
        raise NotAllowed(BUSY_MESSAGE.format(run_id=run_id))
    try:
        cloud_ref, admin_ref = _save_inputs(run_id, start)
    except BaseException:
        _release(run_id)
        raise
    # 스레드에는 비밀값 없이 이름표만 넘긴다.
    threading.Thread(
        target=_run_in_background,
        args=(run_id, start.region, start.target, start.source, start.options, cloud_ref, admin_ref),
        kwargs={"discover": discover, "agent": agent, "ctx": ctx},
        name=f"cdc-run-{run_id}", daemon=True,
    ).start()
    return run_id


def status(run_id: str, *, agent: CdcAgentClient | None = None) -> RunStatus:
    with _active_lock:
        progress = _runs.get(run_id)
    if progress is not None and not progress.job_registered:
        return _status_from_memory(run_id, progress)

    job_id = to_job_id(run_id)
    try:
        job = (agent or _default_agent()).get_job(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            if progress is not None:
                return _status_from_memory(run_id, progress)
            if not _remaining(run_id, None):
                raise UnknownRun(run_id) from None
            return _unknown_status(run_id, "this app has no record of the run, but its resources remain")
        if exc.status_code < 500:
            raise
        return _unknown_status(run_id, _clean(run_id, str(exc)))
    except httpx.HTTPError:
        return _unknown_status(run_id, "CDC agent unreachable")

    message = job.get("message") or (progress.message if progress else None)
    remaining = _remaining(run_id, job)
    return RunStatus(
        run_id=run_id, job_id=job_id, state=job["state"], phase=job["phase"], progress=job["progress"],
        message=_clean(run_id, message) if message else None, metrics=job.get("metrics", {}),
        ready_for_cutover=job["phase"] == READY_PHASE and job["state"] == "waiting_approval",
        cleanup_required=job["state"] in CLEANUP_STATES and bool(remaining), remaining=remaining,
    )


def cutover(run_id: str, approved_by: str, *, agent: CdcAgentClient | None = None) -> CutoverResult:
    """cutover_ready에서만 시작한다. 진행 중이면 다시 시작하지 않고 그 결과를 돌려준다."""
    if not approved_by.strip():
        raise InvalidInput(["approved_by"])
    with _active_lock:
        pending = _cutovers.get(run_id)
        owner = pending is None
        if owner:
            pending = _cutovers[run_id] = Future()
    if not owner:
        return pending.result()
    try:
        result = _start_or_join_cutover(run_id, approved_by, agent or _default_agent())
    except BaseException as exc:
        pending.set_exception(exc)
        raise
    else:
        pending.set_result(result)
        return result
    finally:
        with _active_lock:
            _cutovers.pop(run_id, None)


def finish(run_id: str, *, agent: CdcAgentClient | None = None) -> FinishResult:
    """전환 후 안정화 단계에서 부른다. 오래 걸린다(VPN 삭제). 실패하면 남은 항목과 함께 돌아오며 다시 부를 수 있다."""
    agent = agent or _default_agent()
    with _cleanup_turn(run_id):
        return _finish(run_id, agent)


def stop(run_id: str, *, agent: CdcAgentClient | None = None) -> FinishResult:
    """전환 전에만 된다. 전환이 시작된 작업은 NotAllowed. 정리에 실패하면 남은 항목과 함께 돌아온다."""
    agent = agent or _default_agent()
    with _cleanup_turn(run_id):
        return _stop(run_id, agent)


# ---- run의 백그라운드 절차 ----


def _run_in_background(run_id: str, region: str, target: TargetInput, source: SourceInput | None,
                       options: MigrationOptions, cloud_ref: str, admin_ref: str, *,
                       discover: Discover | None, agent: CdcAgentClient | None, ctx: Reporter | None) -> None:
    job_id = to_job_id(run_id)
    log = ProgressLog(run_id, job_id)

    def stage(name: str) -> None:
        _set_progress(run_id, stage=name)
        log.event("stage", stage=name)

    try:
        client = agent or _default_agent()

        stage("agent_check")
        preflight.check_agent(client)

        stage("source_lookup")
        mig = _load_migration(run_id, region, target, source, options, cloud_ref, admin_ref, discover)

        stage("preflight")
        creds = resolve_inputs(mig)
        preflight.check_source(mig, creds.source_admin["user"])
        if not mig.options.skip_vpn:
            preflight.check_networks(mig, vpn.OciSettings.from_env())
        _claim_source(run_id, mig)
        preflight.check_not_running(mig, job_id, client)

        repl = ensure_repl_account(run_id)
        preflight.check_job_secrets(job_secrets(creds, repl))
        result = run_migration(
            run_id, mig.source.model_dump(), mig.target.model_dump(),
            agent=client, steps=_build_steps(mig, creds, repl, client, log), ctx=ctx,
            log_interval_s=mig.options.log_interval_s, max_hold_ms=mig.options.max_hold_ms, log=log,
            stop_requested=lambda: run_id in _stop_requested,
            on_stage=lambda name: _on_run_stage(run_id, name, stage),
        )
        _set_progress(run_id, state=result["state"])
    except Exception as exc:  # noqa: BLE001 - 스레드 밖으로 나갈 곳이 없으므로 status에 남긴다
        message = _clean(run_id, str(exc))
        logger.warning("run %s failed", run_id)
        log.event("run.ended", state="failed", error=message)
        _set_progress(run_id, state="failed", message=message)
        _end(ctx, "failed", 0, message)
    finally:
        _release(run_id)


def _load_migration(run_id: str, region: str, target: TargetInput, source: SourceInput | None,
                    options: MigrationOptions, cloud_ref: str, admin_ref: str,
                    discover: Discover | None) -> DbMigrationInput:
    if source is not None:
        data = source.model_dump()
    else:
        found = discover(SourceLookup(run_id=run_id, region=region, target_region=target.region,
                                      cloud_ref=cloud_ref, db_admin_ref=admin_ref))
        data = found.model_dump() if hasattr(found, "model_dump") else dict(found)
    tables = [t for t in data.get("tables") or [] if not t.startswith(INTERNAL_SCHEMA_PREFIX)]
    data = {**data, "region": region, "tables": tables,
            "credentials_ref": {"cloud": cloud_ref, "db_admin": admin_ref}}
    try:
        return DbMigrationInput(run_id=run_id, source=data, target=target, options=options)
    except ValidationError as exc:
        raise InvalidInput(_input_problems(exc)) from None


def _on_run_stage(run_id: str, name: str, stage: Callable[[str], None]) -> None:
    stage(name)
    if name == "job":
        _set_progress(run_id, job_registered=True)


def run_migration(
    run_id: str,
    source: dict,
    target: dict,
    *,
    agent: CdcAgentClient,
    steps: "Steps",
    ctx: Reporter | None = None,
    poll_s: float | None = None,
    log_interval_s: float | None = None,
    max_hold_ms: int | None = None,
    log: ProgressLog | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    on_stage: Callable[[str], None] = lambda name: None,
) -> dict:
    """VPN → 접속 확인 → 비밀 전달 → 작업 등록 → 복제 지켜보기.

    멈추는 경우:
    - stop: 도는 run이 aborted를 보고 VPN을 지운다. VPN 생성 중이면 그 단계가 끝난 뒤 작업 없이 지운다.
    - 전환 완료(stabilizing 이후): VPN을 두고 끝난다. 정리는 finish가 한다.
    - ctx.report()에서 Exception이 아닌 예외 발생: 정리 후 그 예외를 다시 던진다.
    - Exception: 정리하지 않는다. 같은 run_id로 다시 부르거나 stop한다.
    """
    job_id = to_job_id(run_id)
    log = log or ProgressLog(run_id, job_id)
    settings = tunables()
    poll_s = settings.job_poll_s if poll_s is None else poll_s
    log_interval_s = settings.log_interval_s if log_interval_s is None else log_interval_s

    def report(progress: int, metrics: dict | None = None, message: str | None = None) -> None:
        if ctx is not None:
            ctx.report("running", progress, metrics, message)

    def check_stop() -> None:
        # 작업 등록 전에는 에이전트로 멈출 수 없다.
        if stop_requested():
            raise _StopBeforeJob()

    def finish_run(job: dict) -> dict:
        vpn_destroyed, vpn_error = _destroy_vpn(steps, job_id, run_id)
        log.event("run.stopped", vpn_destroyed=vpn_destroyed)
        return _run_result(run_id, "stopped", job, vpn_destroyed, vpn_error)

    job_may_exist = False
    try:
        request = build_job_request(job_id, source, target, max_hold_ms)
        log.event("run.start")
        _refuse_finished(agent, job_id)
        check_stop()

        on_stage("vpn")
        report(0, message="VPN 준비")
        steps.ensure_vpn(job_id, source, target)
        check_stop()

        on_stage("probe")
        report(1, message="DB 접속 확인")
        steps.probe(source, target)
        check_stop()

        on_stage("secrets")
        steps.deliver_secrets(job_id, source, target)
        job_may_exist = True
        job = agent.create_job(request)
        on_stage("job")
        log.event("job.created")
        # 등록 직전에 온 stop은 404로 끝났으므로 여기서 정리한다.
        if stop_requested():
            _abort(agent, job_id)
        elif job.get("state") == "failed":
            agent.retry(job_id)  # 같은 run_id 재실행: 실패한 단계부터 이어 간다.

        job = _follow(agent, job_id, report, poll_s, log_interval_s, log, run_id)
        if job["state"] == "aborted":
            return finish_run(job)
        log.event("run.switched", phase=job["phase"])
        return _run_result(run_id, job["phase"], job, False, None)
    except _StopBeforeJob:
        log.event("run.stop_before_job")
        return finish_run({})
    except Exception as exc:
        # 같은 run_id로 다시 부를 수 있게 남겨 둔다.
        log.event("run.failed", error=_clean(run_id, str(exc)))
        # 작업 없이 stop이 왔으면 VPN은 여기서 지운다.
        if stop_requested() and not job_may_exist:
            return finish_run({})
        raise
    except BaseException:
        # 호출 쪽 취소. 정리 중 report를 부르면 취소가 다시 발생한다.
        log.event("run.cancelled")
        _cleanup_after_cancel(agent, steps, job_id, run_id)
        raise


def _follow(
    agent: CdcAgentClient,
    job_id: str,
    report: Callable[..., None],
    poll_s: float,
    log_interval_s: float,
    log: ProgressLog,
    run_id: str,
) -> dict:
    """작업이 중단되거나 전환이 끝나면 돌아오고, 실패하면 던진다."""
    last_phase = None
    last_metrics = 0.0
    app_deploy_logged = False
    while True:
        try:
            job = agent.get_job(job_id)
        except (httpx.HTTPError, AgentError) as exc:
            # CDC는 에이전트에서 계속 돈다. 일시적 연결 오류로 run을 끝내지 않는다.
            if isinstance(exc, AgentError) and exc.status_code < 500:
                raise
            log.event("job.read_error", error=_clean(run_id, str(exc)))
            time.sleep(poll_s)
            continue

        if job["state"] == "aborted" or job["phase"] in SWITCHED_PHASES:
            return job
        if job["state"] == "failed":
            raise AgentError(409, f"job {job_id} failed in {job['phase']}: {job['message']}")

        if job["phase"] != last_phase:
            last_phase = job["phase"]
            log.event("phase", phase=job["phase"], state=job["state"], progress=job["progress"],
                      message=job.get("message"))
        if job["phase"] in APP_DEPLOY_PHASES and not app_deploy_logged:
            app_deploy_logged = True
            log.event("app.deploy_ready", phase=job["phase"])

        ready = job["phase"] == READY_PHASE
        if job["phase"] in METRIC_PHASES and time.monotonic() - last_metrics >= log_interval_s:
            last_metrics = time.monotonic()
            log.event("metrics", **{**job.get("metrics", {}), "phase": job["phase"], "progress": job["progress"]})
        # metrics의 phase는 한 단계 늦을 수 있다.
        report(job["progress"], {**job.get("metrics", {}), "phase": job["phase"]}, job.get("message") or None)
        time.sleep(log_interval_s if ready else poll_s)


# ---- 컷오버 ----


def _start_or_join_cutover(run_id: str, approved_by: str, agent: CdcAgentClient) -> CutoverResult:
    job_id = to_job_id(run_id)
    try:
        job = agent.get_job(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            raise UnknownRun(run_id) from None
        raise
    if job["phase"] == READY_PHASE and job["state"] == "waiting_approval":
        try:
            # 에이전트가 Worker 설정과 연결을 먼저 확인하고, 안 되면 409로 거절한다.
            agent.cutover(job_id, approved_by)
        except AgentError as exc:
            if exc.status_code != 409:
                raise
            job = agent.get_job(job_id)  # 다른 호출이 먼저 시작했을 수 있다.
            if job["phase"] == READY_PHASE:
                raise NotAllowed(_clean(run_id, exc.detail)) from None
    elif job["phase"] != CUTOVER_PHASE and job["phase"] not in SWITCHED_PHASES:
        raise NotAllowed(f"cutover needs phase {READY_PHASE}, the job is {job['state']}/{job['phase']}")
    return _wait_cutover(run_id, job, agent)


def _wait_cutover(run_id: str, job: dict, agent: CdcAgentClient) -> CutoverResult:
    settings = tunables()
    job_id = to_job_id(run_id)
    deadline = time.monotonic() + settings.cutover_wait_s
    while True:
        try:
            job = agent.get_job(job_id)
        except (httpx.HTTPError, AgentError) as exc:
            # 전환은 에이전트에서 계속된다.
            if isinstance(exc, AgentError) and exc.status_code < 500:
                raise
        else:
            outcome = _cutover_outcome(job)
            if outcome:
                return _cutover_result(run_id, job, outcome)
        if time.monotonic() > deadline:
            return _cutover_result(run_id, job, "timed_out")
        time.sleep(settings.cutover_poll_s)


def _cutover_steps(job: dict) -> list[str]:
    cutover_evidence = (job.get("evidence") or {}).get("cutover") or {}
    return [step["step"] for step in cutover_evidence.get("steps", [])]


def _switched(job: dict) -> bool:
    return "switched_to_target" in _cutover_steps(job)


def _cutover_outcome(job: dict) -> str | None:
    if job["state"] in ("failed", "aborted"):
        return "manual_check"
    if job["phase"] in SWITCHED_PHASES:
        return "switched"
    steps = _cutover_steps(job)
    # failed 기록 없는 대기 상태는 전환 시작이 아직 읽히지 않은 것이다.
    if job["phase"] == READY_PHASE and "failed" in steps:
        # 쓰기 차단 해제 같은 복귀 단계가 실패하면 원본이 막혀 있을 수 있다.
        return "manual_check" if any(s.endswith("_failed") for s in steps) else "returned_to_source"
    return None


CUTOVER_MESSAGES = {
    "switched": "전환이 끝났습니다. 전환 시점 데이터 비교(metrics의 verification)와 안정화를 확인한 뒤 finish를 부르세요.",
    "returned_to_source": "전환하지 못해 원본으로 복귀했습니다. 쓰기는 원본으로 갑니다. 원인을 확인한 뒤 다시 부를 수 있습니다.",
    "manual_check": "자동으로 복구하지 못했습니다. 트래픽 방향과 원본 쓰기 차단을 직접 확인하세요.",
    "timed_out": "기다리는 시간이 지났습니다. 전환은 끝나지 않았을 수 있으니 status로 확인하세요.",
}


def _cutover_result(run_id: str, job: dict, outcome: str) -> CutoverResult:
    cutover_evidence = (job.get("evidence") or {}).get("cutover") or {}
    detail = job.get("message") or ""
    return CutoverResult(
        run_id=run_id, job_id=to_job_id(run_id), outcome=outcome, state=job["state"], phase=job["phase"],
        message=CUTOVER_MESSAGES[outcome], detail=_clean(run_id, detail) or None,
        hold_ms=cutover_evidence.get("hold_ms"), evidence=job.get("evidence", {}),
    )


# ---- 정리 ----


@contextmanager
def _cleanup_turn(run_id: str) -> Iterator[None]:
    """HTTP 시간 초과 뒤 다시 부른 호출이 도는 정리와 겹치지 않게 한다."""
    with _active_lock:
        if run_id in _cleaning:
            raise NotAllowed(f"cleanup of run {run_id} is already in progress; check status")
        _cleaning.add(run_id)
    try:
        yield
    finally:
        with _active_lock:
            _cleaning.discard(run_id)


def _finish(run_id: str, agent: CdcAgentClient) -> FinishResult:
    job_id = to_job_id(run_id)
    try:
        job = agent.get_job(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            raise NotAllowed("no job was registered; use stop") from None
        raise
    closed = job["state"] == "succeeded" and job["phase"] == CLOSED_PHASE
    if job["state"] == "failed" and _switched(job):
        # TODO: 전환 후 실패한 작업의 정리 경로. 목적지에 쓰기가 들어갔을 수 있어 사람이 판단한다.
        raise NotAllowed("the job failed after switching to the target; check it by hand before cleaning up")
    if job["phase"] != STABILIZING_PHASE and not closed:
        raise NotAllowed(f"finish needs phase {STABILIZING_PHASE}, the job is {job['state']}/{job['phase']}; "
                         "before cutover use stop")
    try:
        if not closed:
            job = agent.close(job_id)
        _wait_for_run_end(run_id)
        _destroy(run_id)
        _remove_inputs(run_id)
    except Exception as exc:  # noqa: BLE001 - 남은 항목과 함께 돌려준다
        logger.warning("finish failed for %s", run_id)
        return _cleanup_result(run_id, job, _clean(run_id, str(exc)))
    ProgressLog(run_id, job_id).event("run.finished")
    return _cleanup_result(run_id, job, None)


def _stop(run_id: str, agent: CdcAgentClient) -> FinishResult:
    job = None
    try:
        _stop_job(run_id, agent)
        _wait_for_run_end(run_id)
        if has_secret(_ref(run_id, VPN_CONTEXT)):
            raise RuntimeError(f"VPN of run {run_id} was not removed; call stop again")
        _remove_inputs(run_id)
        job = _job_or_none(agent, to_job_id(run_id))
    except AgentError as exc:
        if exc.status_code == 409:
            raise NotAllowed(_clean(run_id, exc.detail)) from None
        return _cleanup_result(run_id, _last_known_job(agent, run_id), _clean(run_id, str(exc)))
    except Exception as exc:  # noqa: BLE001 - 남은 항목과 함께 돌려준다
        logger.warning("stop failed for %s", run_id)
        return _cleanup_result(run_id, _last_known_job(agent, run_id), _clean(run_id, str(exc)))
    return _cleanup_result(run_id, job, None)


def _cleanup_result(run_id: str, job: dict | None, error: str | None) -> FinishResult:
    remaining = _remaining(run_id, job)
    cleanup = (job or {}).get("evidence", {}).get("cleanup", {})
    done = error is None and not remaining
    accounts = cleanup.get("target_accounts", {})
    next_steps = [IAM_NEXT_STEP] if done else []
    if accounts.get("error"):
        # 에이전트가 관리자 비밀을 이미 지웠으므로 다시 불러도 해결되지 않는다.
        next_steps.append(TARGET_ACCOUNTS_NEXT_STEP.format(accounts=", ".join(accounts.get("accounts", []))))
    if ((job or {}).get("evidence", {}).get("verification") or {}).get("state") == "unverifiable":
        next_steps.append(UNVERIFIED_NEXT_STEP)
    if done and (refs := _other_refs(run_id)):
        next_steps.append(OTHER_REFS_NEXT_STEP.format(refs=", ".join(refs)))
    return FinishResult(
        run_id=run_id, job_id=to_job_id(run_id), state="finished" if done else "incomplete",
        message=error, cleanup=cleanup, target_accounts=accounts,
        vpn="kept" if has_secret(_ref(run_id, VPN_CONTEXT)) else "removed",
        remaining=remaining, next_steps=next_steps,
    )


def _remaining(run_id: str, job: dict | None) -> list[str]:
    """정리가 남은 항목의 이름. 값은 담지 않는다."""
    items = []
    if job is not None:
        if job["state"] not in ENDED_STATES:
            items.append("agent_job")
    if has_secret(_ref(run_id, VPN_CONTEXT)):
        items.append("vpn")
    return items + [f"secret:{tag}" for tag in SECRET_TAGS if has_secret(_ref(run_id, tag))]


def _other_refs(run_id: str) -> list[str]:
    ref = _ref(run_id, OTHER_REFS)
    if not has_secret(ref):
        return []
    return [other for other in json.loads(resolve(ref)["refs"]) if has_secret(other)]


def _remove_inputs(run_id: str) -> None:
    for tag in INPUT_SECRETS:
        delete_secret(_ref(run_id, tag))


def stop_job(run_id: str, *, agent: CdcAgentClient | None = None) -> dict:
    """에이전트 쪽 정리 후 VPN 삭제. 입력 비밀값은 남긴다.

    실행 중인 run은 aborted를 보고 VPN을 직접 지운다. VPN을 만드는 중이면 그 단계가 끝난 뒤 멈춘다.
    run이 없으면(실패했거나 앱이 다시 시작됨) 여기서 지운다. 정리에 실패하면 예외를 던지고 VPN을 남긴다.
    """
    agent = agent or _default_agent()
    with _cleanup_turn(run_id):
        return _stop_job(run_id, agent)


def _stop_job(run_id: str, agent: CdcAgentClient) -> dict:
    with _active_lock:
        alive = run_id in _active
        if alive:
            _stop_requested.add(run_id)
    try:
        stop_migration(run_id, agent=agent, steps=Steps(destroy_vpn=lambda job_id: _destroy(run_id)), run_alive=alive)
    except Exception:
        # 정리 실패: run이 VPN을 지우면 안 된다.
        with _active_lock:
            _stop_requested.discard(run_id)
        raise
    return {"run_id": run_id, "job_id": to_job_id(run_id), "vpn": "removed by the run" if alive else "removed"}


def complete(run_id: str, *, agent: CdcAgentClient | None = None) -> dict:
    """stop_job 후 입력 비밀값까지 지운다. 실패하면 예외를 던지며 다시 부를 수 있다."""
    agent = agent or _default_agent()
    with _cleanup_turn(run_id):
        _stop_job(run_id, agent)
        _wait_for_run_end(run_id)
        # _destroy가 마지막에 지우므로, VPN을 지우지 못했으면 아직 남아 있다.
        if has_secret(_ref(run_id, VPN_CONTEXT)):
            raise RuntimeError(f"VPN of run {run_id} was not removed; call complete again")
        _remove_inputs(run_id)
    removed = sorted(INPUT_SECRETS)
    job_id = to_job_id(run_id)
    ProgressLog(run_id, job_id).event("run.completed", secrets_removed=removed)
    return {"run_id": run_id, "job_id": job_id, "secrets_removed": removed}


def stop_migration(run_id: str, *, agent: CdcAgentClient, steps: "Steps", run_alive: bool) -> None:
    """run_alive가 False면 VPN도 여기서 지운다."""
    job_id = to_job_id(run_id)
    _abort(agent, job_id)
    if not run_alive:
        destroyed, error = _destroy_vpn(steps, job_id, run_id)
        if not destroyed:
            raise RuntimeError(f"VPN을 지우지 못했습니다: {error}")


def _wait_for_run_end(run_id: str) -> None:
    settings = tunables()
    deadline = time.monotonic() + settings.complete_wait_s
    while True:
        with _active_lock:
            if run_id not in _active:
                return
        if time.monotonic() > deadline:
            raise RuntimeError(f"run {run_id} is still removing the VPN; call complete again")
        time.sleep(settings.complete_poll_s)


def _destroy(run_id: str) -> None:
    """VPN 먼저. VPN을 지우려면 클라우드 키와 터널 비밀이 필요하다."""
    vpn.destroy_vpn(run_id)
    for tag in OWN_SECRETS:
        delete_secret(_ref(run_id, tag))


def _destroy_vpn(steps: "Steps", job_id: str, run_id: str) -> tuple[bool, str | None]:
    try:
        steps.destroy_vpn(job_id)
    except Exception as exc:
        logger.warning("destroy_vpn failed for %s", job_id)
        return False, _clean(run_id, str(exc))
    return True, None


def _cleanup_after_cancel(agent: CdcAgentClient, steps: "Steps", job_id: str, run_id: str) -> None:
    try:
        _abort(agent, job_id)
    except Exception:
        # VPN을 지우면 복제 슬롯을 지울 수 없다. stop으로 다시 정리한다.
        logger.warning("abort failed for %s, keeping the VPN", job_id)
        return
    _destroy_vpn(steps, job_id, run_id)


def _abort(agent: CdcAgentClient, job_id: str) -> None:
    try:
        agent.abort(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            return  # 등록 전
        # 전환 후 실패한 작업(failed)도 409다. 슬롯이 남아 있어 사람이 판단할 때까지 VPN을 지우면 안 된다.
        if exc.status_code != 409 or agent.get_job(job_id)["state"] not in ENDED_STATES:
            raise


# ---- 입력과 비밀값 ----


@dataclass(frozen=True)
class Resolved:
    source_admin: dict[str, str]
    target_admin: dict[str, str]
    target_ddl: dict[str, str]
    target_sink: dict[str, str]


def _save_inputs(run_id: str, start: StartInput) -> tuple[str, str]:
    cloud = {"aws_access_key_id": start.aws_access_key_id.get_secret_value(),
             "aws_secret_access_key": start.aws_secret_access_key.get_secret_value()}
    if start.aws_session_token:
        cloud["aws_session_token"] = start.aws_session_token.get_secret_value()
    cloud_ref = put_secret(f"{run_id}/source-cloud", cloud)
    admin_ref = put_secret(f"{run_id}/source-db-admin", {"user": start.db_user.get_secret_value(),
                                                         "password": start.db_password.get_secret_value()})
    given = start.target.credentials_ref
    others = [ref for ref in (given.cloud, given.admin, given.ddl_user, given.sink_user)
              if ref and not ref.startswith(f"secret://{run_id}/")]
    if others:
        put_secret(f"{run_id}/{OTHER_REFS}", {"refs": json.dumps(others)})
    return cloud_ref, admin_ref


def _ensure_account(ref: str, user: str) -> dict[str, str]:
    try:
        return resolve(ref)
    except KeyError:
        value = {"user": user, "password": secrets.token_hex(24)}
        put_secret(ref, value)
        return value


def _target_account(mig: DbMigrationInput, given: str | None, tag: str, user: str) -> dict[str, str]:
    """이름표를 받았으면 값이 있어야 한다. 없으면 run 아래에 저장된 것을 쓰거나 만든다."""
    return resolve(given) if given else _ensure_account(_ref(mig.run_id, tag), user)


def resolve_inputs(mig: DbMigrationInput) -> Resolved:
    if not mig.options.skip_vpn:
        # VPN이 직접 읽는 값이다. 구축을 시작한 뒤가 아니라 여기서 실패한다.
        resolve(mig.source.credentials_ref.cloud)
        resolve(mig.target.credentials_ref.cloud)
    target = mig.target.credentials_ref
    settings = tunables()
    return Resolved(
        source_admin=resolve(mig.source.credentials_ref.db_admin),
        target_admin=resolve(target.admin),
        target_ddl=_target_account(mig, target.ddl_user, "target-ddl-user", settings.target_ddl_user),
        target_sink=_target_account(mig, target.sink_user, "target-sink-user", settings.target_sink_user),
    )


def ensure_repl_account(run_id: str) -> dict[str, str]:
    return _ensure_account(_ref(run_id, "source-repl"), tunables().repl_user)


def job_secrets(creds: Resolved, repl: dict[str, str]) -> dict[str, str]:
    admin = creds.source_admin
    return {
        "source.user": repl["user"],
        "source.password": repl["password"],
        # 마스터 계정이 원본 앱 계정을 겸한다. 컷오버만 쓴다.
        "source.app.user": admin["user"],
        "source.app.password": admin["password"],
        "source.admin.user": admin["user"],
        "source.admin.password": admin["password"],
        "target.user": creds.target_sink["user"],
        "target.password": creds.target_sink["password"],
        "target.ddl.user": creds.target_ddl["user"],
        "target.ddl.password": creds.target_ddl["password"],
        "target.admin.user": creds.target_admin["user"],
        "target.admin.password": creds.target_admin["password"],
    }


# ---- 클라우드에 닿는 단계 ----


def _not_built(name: str) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{name} 단계가 설정되지 않았습니다")

    return call


@dataclass(frozen=True)
class Steps:
    ensure_vpn: Callable[[str, dict, dict], Any] = _not_built("ensure_vpn")
    probe: Callable[[dict, dict], Any] = _not_built("probe")
    deliver_secrets: Callable[[str, dict, dict], Any] = _not_built("deliver_secrets")
    destroy_vpn: Callable[[str], Any] = _not_built("destroy_vpn")


def _build_steps(mig: DbMigrationInput, creds: Resolved, repl: dict, agent: CdcAgentClient,
                 log: ProgressLog) -> Steps:
    def ensure(job_id: str, source: dict, target: dict) -> None:
        if mig.options.skip_vpn:
            log.event("vpn.skipped")
            return
        log.event("vpn.start")
        started = time.monotonic()
        result = vpn.ensure_vpn(mig, job_id)
        log.event("vpn.ready", seconds=round(time.monotonic() - started), tunnels=result["tunnel_status"],
                  skipped=result.get("skipped", False))

    return Steps(
        ensure_vpn=ensure,
        probe=lambda source, target: _probe(agent, mig, log),
        deliver_secrets=lambda job_id, source, target: agent.put_secrets(job_id, job_secrets(creds, repl)),
        destroy_vpn=lambda job_id: _destroy(mig.run_id),
    )


def _probe(agent: CdcAgentClient, mig: DbMigrationInput, log: ProgressLog) -> None:
    endpoints = [{"host": mig.source.db.host, "port": mig.source.db.port},
                 {"host": mig.target.db.private_ip, "port": mig.target.db.port}]
    settings = tunables()
    # 터널이 올라온 뒤 경로가 퍼지는 데 시간이 걸린다.
    deadline = time.monotonic() + (0 if mig.options.skip_vpn else settings.probe_wait_s)
    while True:
        result = agent.probe(endpoints)
        log.event("probe", result=result)
        unreachable = [f"{r['host']}:{r['port']}" for r in result if not r["ok"]]
        if not unreachable:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"not reachable from the CDC nodes: {', '.join(unreachable)}")
        time.sleep(settings.probe_retry_s)


# ---- 작은 도움 함수 ----


def to_job_id(run_id: str) -> str:
    return run_id.replace("-", "_")


def build_job_request(job_id: str, source: dict, target: dict, max_hold_ms: int | None = None) -> dict:
    """에이전트가 쓰는 값만 꺼낸다. 비밀값은 없다."""
    src, tgt = source["db"], target["db"]
    given = target.get("credentials_ref") or {}
    return JobRequest(
        job_id=job_id,
        source=SourceSpec(
            host=src["host"],
            port=src["port"],
            database=src["database"],
            tables=source["tables"],
            app_role=source["app"]["db_user"],
        ),
        # target ssl_mode는 Cloud SQL 설정값(ENCRYPTED_ONLY)이라 접속 옵션으로 쓰지 않는다.
        target=TargetSpec(
            host=tgt["private_ip"],
            port=tgt["port"],
            database=tgt["database"],
            app_role=target.get("app", {}).get("db_user"),
            # 호출자가 준 계정은 다른 곳에서도 쓸 수 있어 종료 때 로그인을 막지 않는다.
            keep_ddl_user=bool(given.get("ddl_user")),
            keep_sink_user=bool(given.get("sink_user")),
        ),
        options=JobOptions() if max_hold_ms is None else JobOptions(max_hold_ms=max_hold_ms),
    ).model_dump()


def _refuse_ended_run(run_id: str, agent: CdcAgentClient | None) -> None:
    """키를 저장하기 전에 거절한다. 확인하지 못하면 넘어간다. 백그라운드 검사가 결과를 status에 남긴다."""
    try:
        job = _job_or_none(agent or _default_agent(), to_job_id(run_id))
    except (AgentError, httpx.HTTPError, MissingSetting):
        return
    if job is not None and job["state"] in ENDED_STATES:
        raise NotAllowed(f"run {run_id} already ended ({job['state']}); start with a new run_id")


def _refuse_finished(agent: CdcAgentClient, job_id: str) -> None:
    """정리까지 끝난 작업에는 VPN과 비밀을 다시 만들지 않는다."""
    job = _job_or_none(agent, job_id)
    if job is not None and job["state"] in ENDED_STATES:
        raise RuntimeError(f"job {job_id} already ended ({job['state']}); start with a new run_id")


def _job_or_none(agent: CdcAgentClient, job_id: str) -> dict | None:
    try:
        return agent.get_job(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            return None
        raise


def _last_known_job(agent: CdcAgentClient, run_id: str) -> dict | None:
    # 정리 실패를 알리는 중이므로, 작업을 읽지 못해도 그 결과를 돌려준다.
    try:
        return _job_or_none(agent, to_job_id(run_id))
    except (AgentError, httpx.HTTPError):
        return None


def _run_result(run_id: str, state: str, job: dict, vpn_destroyed: bool, vpn_error: str | None) -> dict:
    return {
        "run_id": run_id,
        "job_id": to_job_id(run_id),
        "state": state,
        "vpn_destroyed": vpn_destroyed,
        "vpn_error": vpn_error,
        "outputs": job.get("outputs", {}),
        "evidence": job.get("evidence", {}),
    }


def _status_from_memory(run_id: str, progress: _Progress) -> RunStatus:
    remaining = _remaining(run_id, None)
    return RunStatus(
        run_id=run_id, job_id=to_job_id(run_id), state=progress.state, phase=progress.stage, progress=0,
        message=progress.message,
        cleanup_required=progress.state in CLEANUP_STATES and bool(remaining), remaining=remaining,
    )


def _unknown_status(run_id: str, message: str) -> RunStatus:
    remaining = _remaining(run_id, None)
    return RunStatus(run_id=run_id, job_id=to_job_id(run_id), state="unknown", phase="unknown", progress=0,
                     message=message, cleanup_required=bool(remaining), remaining=remaining)


def _ref(run_id: str, tag: str) -> str:
    return f"secret://{run_id}/{tag}"


def _clean(run_id: str, text: str) -> str:
    values = redact.run_values(run_id, SECRET_TAGS)
    try:
        values += redact.ref_values(_other_refs(run_id))
    except Exception:  # noqa: BLE001 - 비밀값 저장소 오류가 정리 중인 오류를 가리면 안 된다
        logger.warning("run %s: could not read its other references", run_id)
    return redact.clean(text, values)


def _default_agent() -> CdcAgentClient:
    return CdcAgentClient(AgentSettings.from_env())


def _new_run_id() -> str:
    return f"mig-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4)}"


def _input_problems(exc: ValidationError) -> list[str]:
    # 필드 이름만. pydantic 원문은 값을 인용할 수 있다.
    return sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})


def _reserve(run_id: str) -> bool:
    with _active_lock:
        if run_id in _active or run_id in _cleaning:
            return False
        _active.add(run_id)
        _runs[run_id] = _Progress(stage="inputs")
        return True


def _release(run_id: str) -> None:
    with _active_lock:
        _active.discard(run_id)
        _stop_requested.discard(run_id)


def _set_progress(run_id: str, **fields: Any) -> None:
    with _active_lock:
        progress = _runs[run_id]
        for name, value in fields.items():
            setattr(progress, name, value)


def _claim_source(run_id: str, mig: DbMigrationInput) -> None:
    """이 앱의 다른 run이 같은 원본을 쓰는지 본다. 에이전트에 등록 전인 run은 에이전트가 모른다."""
    key = preflight.source_key(mig)
    with _active_lock:
        if any(_runs[other].source == key for other in _active if other != run_id and other in _runs):
            raise preflight.PreflightError("another run on this app already works on the same source DB")
        _runs[run_id].source = key


def _end(ctx: Reporter | None, state: str, progress: int, message: str | None) -> None:
    if ctx is not None:
        ctx.report(state, progress, None, message)


# ---- workflows가 쓰는 기존 호출 ----


def execute(inp: DbMigrationInput | dict, ctx: Reporter | None = None, *,
            agent: CdcAgentClient | None = None) -> DbMigrationResult:
    """입력을 저장·조회하지 않고 받은 그대로 실행하며 끝날 때까지 기다린다. 실패는 state=failed로 돌아온다."""
    try:
        mig = DbMigrationInput.model_validate(inp)
    except ValidationError as exc:
        run_id = str(inp.get("run_id", "unknown")) if isinstance(inp, dict) else "unknown"
        job_id = to_job_id(run_id)
        missing = _input_problems(exc)
        ProgressLog(run_id, job_id).event("inputs.missing", missing=missing)
        message = f"invalid input: {', '.join(missing)}"
        _end(ctx, "failed", 0, message)
        return DbMigrationResult(run_id=run_id, job_id=job_id, state="failed", message=message)
    except MissingSetting as exc:
        # 입력의 기본값을 설정에서 읽다가 난 오류. 변수 이름만 담는다.
        run_id = str(inp.get("run_id", "unknown")) if isinstance(inp, dict) else "unknown"
        _end(ctx, "failed", 0, str(exc))
        return DbMigrationResult(run_id=run_id, job_id=to_job_id(run_id), state="failed", message=str(exc))

    job_id = to_job_id(mig.run_id)
    log = ProgressLog(mig.run_id, job_id)
    if not _reserve(mig.run_id):
        message = BUSY_MESSAGE.format(run_id=mig.run_id)
        return DbMigrationResult(run_id=mig.run_id, job_id=job_id, state="failed", message=message)
    try:
        log.event("inputs.loaded", source_host=mig.source.db.host, target_host=mig.target.db.private_ip,
                  tables=len(mig.source.tables), skip_vpn=mig.options.skip_vpn)
        creds = resolve_inputs(mig)
        preflight.check_app_account(mig, creds.source_admin["user"])
        client = agent or _default_agent()
        if not mig.options.skip_vpn:
            # 첫 단계인 VPN이 15~20분 걸린다. 고장 난 클러스터나 겹치는 대역은 그 전에 멈춘다.
            preflight.check_agent(client)
            preflight.check_networks(mig, vpn.OciSettings.from_env())
        repl = ensure_repl_account(mig.run_id)
        preflight.check_job_secrets(job_secrets(creds, repl))
        result = run_migration(
            mig.run_id, mig.source.model_dump(), mig.target.model_dump(),
            agent=client, steps=_build_steps(mig, creds, repl, client, log), ctx=ctx,
            log_interval_s=mig.options.log_interval_s, max_hold_ms=mig.options.max_hold_ms, log=log,
            stop_requested=lambda: mig.run_id in _stop_requested,
            on_stage=lambda name: _on_run_stage(mig.run_id, name, lambda stage: _set_progress(mig.run_id, stage=stage)),
        )
    except Exception as exc:
        # run_migration이 본 것은 기록했다. 입력·비밀값 문제는 여기서 기록한다.
        message = _clean(mig.run_id, str(exc))
        log.event("run.ended", state="failed", error=message)
        _set_progress(mig.run_id, state="failed", message=message)
        _end(ctx, "failed", 0, message)
        return DbMigrationResult(run_id=mig.run_id, job_id=job_id, state="failed", message=message)
    finally:
        _release(mig.run_id)

    _set_progress(mig.run_id, state=result["state"])
    message = f"VPN not removed: {result['vpn_error']}" if result["vpn_error"] else None
    _end(ctx, result["state"], 100, message)
    vpn_view = {"destroyed": result["vpn_destroyed"], "error": result["vpn_error"]}
    return DbMigrationResult(
        run_id=mig.run_id, job_id=job_id, state=result["state"], message=message,
        outputs=result["outputs"], evidence=result["evidence"], vpn=vpn_view,
    )
