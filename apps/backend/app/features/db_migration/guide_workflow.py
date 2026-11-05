"""run·stop의 실행 순서. workflows는 service.run·service.stop을 부른다.

VPN·비밀 전달·접속 확인 구현은 service.py가 Steps로 넣는다.

멈추는 방법:
- service.stop(): 도는 run이 aborted를 보고 VPN을 지운다. VPN 생성 중이면 그 단계가 끝난 뒤 작업 없이 지운다.
- ctx.report()에서 Exception이 아닌 예외 발생: 정리 후 그 예외를 다시 던진다.

Exception으로 끝나면 정리하지 않는다. 같은 run_id로 다시 부르거나 stop한다.
정리에 실패하면 VPN을 남긴다. VPN 없이는 원본 복제 슬롯을 지울 수 없다.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .client import AgentError, CdcAgentClient
from .progress_log import ProgressLog
from .schemas import JobOptions, JobRequest, SourceSpec, TargetSpec

logger = logging.getLogger(__name__)

READY_PHASE = "cutover_ready"
METRIC_PHASES = {"streaming", READY_PHASE}
# 복사가 빨리 끝나면 streaming 없이 READY_PHASE로 간다.
APP_DEPLOY_PHASES = {"streaming", READY_PHASE}
TERMINAL_STATES = {"succeeded", "failed", "aborted"}


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


def _not_built(name: str) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{name} 단계가 설정되지 않았습니다")

    return call


@dataclass(frozen=True)
class Steps:
    """클라우드에 닿는 단계. 구현은 service.py에 있다."""

    ensure_vpn: Callable[[str, dict, dict], Any] = _not_built("ensure_vpn")
    probe: Callable[[dict, dict], Any] = _not_built("probe")
    deliver_secrets: Callable[[str, dict, dict], Any] = _not_built("deliver_secrets")
    destroy_vpn: Callable[[str], Any] = _not_built("destroy_vpn")


def to_job_id(run_id: str) -> str:
    return run_id.replace("-", "_")


def build_job_request(job_id: str, source: dict, target: dict, max_hold_ms: int = 3000) -> dict:
    """에이전트가 쓰는 값만 꺼낸다. 비밀값은 없다."""
    src, tgt = source["db"], target["db"]
    return JobRequest(
        job_id=job_id,
        source=SourceSpec(
            host=src["host"],
            port=src["port"],
            database=src["database"],
            tables=source["tables"],
            app_role=source["app"]["db_user"],
        ),
        target=TargetSpec(
            host=tgt["private_ip"],
            port=tgt["port"],
            database=tgt["database"],
            # ssl_mode는 Cloud SQL 설정값(ENCRYPTED_ONLY)이라 접속 옵션으로 쓰지 않는다.
            sslmode="require",
            app_role=target.get("app", {}).get("db_user"),
        ),
        options=JobOptions(max_hold_ms=max_hold_ms),
    ).model_dump()


def run_migration(
    run_id: str,
    source: dict,
    target: dict,
    *,
    agent: CdcAgentClient,
    steps: Steps,
    ctx: Reporter | None = None,
    poll_s: float = 3,
    log_interval_s: float = 10,
    max_hold_ms: int = 3000,
    log: ProgressLog | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
) -> dict:
    job_id = to_job_id(run_id)
    log = log or ProgressLog(run_id, job_id)

    def report(progress: int, metrics: dict | None = None, message: str | None = None) -> None:
        if ctx is not None:
            ctx.report("running", progress, metrics, message)

    def check_stop() -> None:
        # 작업 등록 전에는 에이전트로 멈출 수 없다.
        if stop_requested():
            raise _StopBeforeJob()

    def finish(job: dict) -> dict:
        vpn_destroyed, vpn_error = _destroy_vpn(steps, job_id)
        log.event("run.stopped", vpn_destroyed=vpn_destroyed)
        return {
            "run_id": run_id,
            "job_id": job_id,
            "state": "stopped",
            "vpn_destroyed": vpn_destroyed,
            "vpn_error": vpn_error,
            "outputs": job.get("outputs", {}),
            "evidence": job.get("evidence", {}),
        }

    job_may_exist = False
    try:
        request = build_job_request(job_id, source, target, max_hold_ms)
        log.event("run.start")
        _refuse_finished(agent, job_id)

        report(0, message="VPN 준비")
        steps.ensure_vpn(job_id, source, target)
        check_stop()

        report(1, message="DB 접속 확인")
        steps.probe(source, target)
        check_stop()

        steps.deliver_secrets(job_id, source, target)
        job_may_exist = True
        job = agent.create_job(request)
        log.event("job.created")
        # 등록 직전에 온 stop은 404로 끝났으므로 여기서 정리한다.
        if stop_requested():
            _abort(agent, job_id)
        elif job.get("state") == "failed":
            agent.retry(job_id)  # 같은 run_id 재실행: 실패한 단계부터 이어 간다.

        return finish(_follow(agent, job_id, report, poll_s, log_interval_s, log))
    except _StopBeforeJob:
        log.event("run.stop_before_job")
        return finish({})
    except Exception as exc:
        # 같은 run_id로 다시 부를 수 있게 남겨 둔다.
        log.event("run.failed", error=str(exc))
        # 작업 없이 stop이 왔으면 VPN은 여기서 지운다.
        if stop_requested() and not job_may_exist:
            return finish({})
        raise
    except BaseException:
        # 호출 쪽 취소. 정리 중 report를 부르면 취소가 다시 발생한다.
        log.event("run.cancelled")
        _cleanup_after_cancel(agent, steps, job_id)
        raise


def stop_migration(run_id: str, *, agent: CdcAgentClient, steps: Steps, run_alive: bool) -> None:
    """1차 종료. run_alive가 False면 VPN도 여기서 지운다."""
    job_id = to_job_id(run_id)
    _abort(agent, job_id)
    if not run_alive:
        destroyed, error = _destroy_vpn(steps, job_id)
        if not destroyed:
            raise RuntimeError(f"VPN을 지우지 못했습니다: {error}")


def _refuse_finished(agent: CdcAgentClient, job_id: str) -> None:
    """정리까지 끝난 작업에는 VPN과 비밀을 다시 만들지 않는다."""
    try:
        job = agent.get_job(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            return
        raise
    if job["state"] in ("aborted", "succeeded"):
        raise RuntimeError(f"job {job_id} already ended ({job['state']}); start with a new run_id")


def _follow(
    agent: CdcAgentClient,
    job_id: str,
    report: Callable[..., None],
    poll_s: float,
    log_interval_s: float,
    log: ProgressLog,
) -> dict:
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
            log.event("job.read_error", error=str(exc))
            time.sleep(poll_s)
            continue

        if job["state"] == "aborted":
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


def _abort(agent: CdcAgentClient, job_id: str) -> None:
    try:
        agent.abort(job_id)
    except AgentError as exc:
        if exc.status_code == 404:
            return  # 등록 전
        if exc.status_code != 409 or agent.get_job(job_id)["state"] not in TERMINAL_STATES:
            raise  # 전환 단계의 작업 등은 멈추면 안 된다.


def _destroy_vpn(steps: Steps, job_id: str) -> tuple[bool, str | None]:
    try:
        steps.destroy_vpn(job_id)
    except Exception as exc:
        logger.exception("destroy_vpn failed for %s", job_id)
        return False, str(exc)
    return True, None


def _cleanup_after_cancel(agent: CdcAgentClient, steps: Steps, job_id: str) -> None:
    try:
        _abort(agent, job_id)
    except Exception:
        # VPN을 지우면 복제 슬롯을 지울 수 없다. stop으로 다시 정리한다.
        logger.exception("abort failed for %s, keeping the VPN", job_id)
        return
    _destroy_vpn(steps, job_id)
