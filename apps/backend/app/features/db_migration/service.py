"""Entry point of the module: run, stop and complete. workflows calls these.

run goes from the inputs to live CDC and keeps it open until stop (first phase: no cutover).
complete is stop plus removing the saved inputs, for when the work is declared finished.
Progress goes to the backend log (progress_log.py) and to ctx when given.
"""

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.secrets import delete_secret, has_secret, put_secret, resolve

from . import vpn
from .client import AgentSettings, CdcAgentClient
from .guide_workflow import Reporter, Steps, run_migration, stop_migration, to_job_id
from .input_secrets import INPUT_SECRETS
from .progress_log import ProgressLog
from .schemas import DbMigrationInput, DbMigrationResult

REPL_USER = "alpha_cdc_repl"
TARGET_DDL_USER = "alpha_ddl"
TARGET_SINK_USER = "alpha_sink"
PROBE_WAIT_S = 180
PROBE_RETRY_S = 10
# A live run removes the VPN itself after stop, which can take many minutes.
COMPLETE_WAIT_S = 1800
COMPLETE_POLL_S = 5
# Made by this module. Inputs from assessment and provisioning belong to the platform.
OWN_SECRETS = ("source-repl", "vpn-psk", "vpn-context")

_active: set[str] = set()
# Stops for live runs. Before the job exists, nothing else reaches the run.
_stop_requested: set[str] = set()
_active_lock = threading.Lock()


@dataclass(frozen=True)
class Resolved:
    source_admin: dict[str, str]
    target_admin: dict[str, str]
    target_ddl: dict[str, str]
    target_sink: dict[str, str]


def _ensure_account(ref: str, user: str) -> dict[str, str]:
    try:
        return resolve(ref)
    except KeyError:
        value = {"user": user, "password": secrets.token_hex(24)}
        put_secret(ref, value)
        return value


def _target_account(mig: DbMigrationInput, given: str | None, tag: str, user: str) -> dict[str, str]:
    """A reference that was given must exist; otherwise use what is saved under the run, or make it."""
    return resolve(given) if given else _ensure_account(f"secret://{mig.run_id}/{tag}", user)


def resolve_inputs(mig: DbMigrationInput) -> Resolved:
    if not mig.options.skip_vpn:
        # VPN reads these itself; fail here rather than after it starts building.
        resolve(mig.source.credentials_ref.cloud)
        resolve(mig.target.credentials_ref.cloud)
    target = mig.target.credentials_ref
    return Resolved(
        source_admin=resolve(mig.source.credentials_ref.db_admin),
        target_admin=resolve(target.admin),
        target_ddl=_target_account(mig, target.ddl_user, "target-ddl-user", TARGET_DDL_USER),
        target_sink=_target_account(mig, target.sink_user, "target-sink-user", TARGET_SINK_USER),
    )


def ensure_repl_account(run_id: str) -> dict[str, str]:
    return _ensure_account(f"secret://{run_id}/source-repl", REPL_USER)


def job_secrets(creds: Resolved, repl: dict[str, str]) -> dict[str, str]:
    admin = creds.source_admin
    return {
        "source.user": repl["user"],
        "source.password": repl["password"],
        # The master account also serves as the source app account; only cutover uses it.
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


def _probe(agent: CdcAgentClient, mig: DbMigrationInput, log: ProgressLog) -> None:
    endpoints = [{"host": mig.source.db.host, "port": mig.source.db.port},
                 {"host": mig.target.db.private_ip, "port": mig.target.db.port}]
    # Routes can take a moment to arrive after the tunnels come up.
    deadline = time.monotonic() + (0 if mig.options.skip_vpn else PROBE_WAIT_S)
    while True:
        result = agent.probe(endpoints)
        log.event("probe", result=result)
        unreachable = [f"{r['host']}:{r['port']}" for r in result if not r["ok"]]
        if not unreachable:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"not reachable from the CDC nodes: {', '.join(unreachable)}")
        time.sleep(PROBE_RETRY_S)


def _destroy(run_id: str) -> None:
    """VPN first: removing it needs the cloud keys and tunnel secrets."""
    vpn.destroy_vpn(run_id)
    for tag in OWN_SECRETS:
        delete_secret(f"secret://{run_id}/{tag}")


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


def _input_problems(exc: ValidationError) -> list[str]:
    # Field names only; pydantic's own text can quote the values.
    return sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})


def _end(ctx: Reporter | None, state: str, progress: int, message: str | None) -> None:
    if ctx is not None:
        ctx.report(state, progress, None, message)


def run(inp: DbMigrationInput | dict, ctx: Reporter | None = None, *,
        agent: CdcAgentClient | None = None) -> DbMigrationResult:
    """Long running; call it in the background. Failures come back as state=failed."""
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

    job_id = to_job_id(mig.run_id)
    log = ProgressLog(mig.run_id, job_id)
    with _active_lock:
        if mig.run_id in _active:
            message = f"run {mig.run_id} is already running"
            return DbMigrationResult(run_id=mig.run_id, job_id=job_id, state="failed", message=message)
        _active.add(mig.run_id)
    try:
        log.event("inputs.loaded", source_host=mig.source.db.host, target_host=mig.target.db.private_ip,
                  tables=len(mig.source.tables), skip_vpn=mig.options.skip_vpn)
        creds = resolve_inputs(mig)
        repl = ensure_repl_account(mig.run_id)
        agent = agent or CdcAgentClient(AgentSettings.from_env())
        result: dict[str, Any] = run_migration(
            mig.run_id, mig.source.model_dump(), mig.target.model_dump(),
            agent=agent, steps=_build_steps(mig, creds, repl, agent, log), ctx=ctx,
            log_interval_s=mig.options.log_interval_s, max_hold_ms=mig.options.max_hold_ms, log=log,
            stop_requested=lambda: mig.run_id in _stop_requested,
        )
    except Exception as exc:
        # run_migration has logged what it saw; input and secret problems are logged here.
        message = str(exc)
        log.event("run.ended", state="failed", error=message)
        _end(ctx, "failed", 0, message)
        return DbMigrationResult(run_id=mig.run_id, job_id=job_id, state="failed", message=message)
    finally:
        with _active_lock:
            _active.discard(mig.run_id)
            _stop_requested.discard(mig.run_id)

    message = None if result["vpn_destroyed"] else f"VPN not removed: {result['vpn_error']}"
    _end(ctx, "stopped", 100, message)
    return DbMigrationResult(
        run_id=mig.run_id, job_id=job_id, state="stopped", message=message,
        outputs=result["outputs"], evidence=result["evidence"],
        vpn={"destroyed": result["vpn_destroyed"], "error": result["vpn_error"]},
    )


def stop(run_id: str, *, agent: CdcAgentClient | None = None) -> dict:
    """End the run: clean up on the agent, then remove the VPN.

    A live run notices aborted on its next read and removes the VPN itself; one still building
    the VPN stops when that step ends. With no live run (it failed, or the app restarted) this
    does it. A failed cleanup raises and keeps the VPN, because the source's replication slot
    can only be removed through it; call stop again.
    """
    with _active_lock:
        alive = run_id in _active
        if alive:
            _stop_requested.add(run_id)
    agent = agent or CdcAgentClient(AgentSettings.from_env())
    try:
        stop_migration(run_id, agent=agent, steps=Steps(destroy_vpn=lambda job_id: _destroy(run_id)), run_alive=alive)
    except Exception:
        # Cleanup failed: the run must keep the VPN.
        with _active_lock:
            _stop_requested.discard(run_id)
        raise
    return {"run_id": run_id, "job_id": to_job_id(run_id), "vpn": "removed by the run" if alive else "removed"}


def _wait_for_run_end(run_id: str) -> None:
    deadline = time.monotonic() + COMPLETE_WAIT_S
    while True:
        with _active_lock:
            if run_id not in _active:
                return
        if time.monotonic() > deadline:
            raise RuntimeError(f"run {run_id} is still removing the VPN; call complete again")
        time.sleep(COMPLETE_POLL_S)


def complete(run_id: str, *, agent: CdcAgentClient | None = None) -> dict:
    """Declare the work finished: stop, then remove the saved inputs of this run from the platform.

    Long running when a run is live; call it in the background. Inputs stay until the VPN is
    gone, because removing it needs the cloud keys; on any failure call complete again.
    The cloud account's own keys are not touched.
    """
    stop(run_id, agent=agent)
    _wait_for_run_end(run_id)
    # _destroy removes this last, so it is still here when the VPN could not be removed.
    if has_secret(f"secret://{run_id}/vpn-context"):
        raise RuntimeError(f"VPN of run {run_id} was not removed; call complete again")
    for tag in INPUT_SECRETS:
        delete_secret(f"secret://{run_id}/{tag}")
    removed = sorted(INPUT_SECRETS)
    job_id = to_job_id(run_id)
    ProgressLog(run_id, job_id).event("run.completed", secrets_removed=removed)
    return {"run_id": run_id, "job_id": job_id, "secrets_removed": removed}
