"""Settings of this module, in one place.

Values that differ per environment have no default here: the run stops and names the variable.
Operating values (waits, account names) have a default and can be overridden by an environment variable.
"""

import os
from dataclasses import dataclass


class MissingSetting(RuntimeError, KeyError):
    """A required environment variable is not set, or its value has the wrong form.

    Also a KeyError: workflows catches that around AgentSettings.from_env.
    """

    __str__ = BaseException.__str__  # KeyError would put quotes around the message


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MissingSetting(f"environment variable {name} is required")
    return value


def _text(name: str, default: str) -> str:
    return os.getenv(name) or default


def _number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise MissingSetting(f"environment variable {name} must be a number") from None


@dataclass(frozen=True)
class Tunables:
    repl_user: str
    target_ddl_user: str
    target_sink_user: str
    db_sslmode: str
    max_hold_ms: int
    log_interval_s: float
    job_poll_s: float
    probe_wait_s: float
    probe_retry_s: float
    complete_wait_s: float
    complete_poll_s: float
    cutover_wait_s: float
    cutover_poll_s: float
    terraform_timeout_s: float
    tunnel_wait_s: float
    tunnel_poll_s: float
    agent_timeout_s: float
    agent_abort_timeout_s: float


def tunables() -> Tunables:
    """Read on each call so a changed environment applies to the next run."""
    return Tunables(
        repl_user=_text("CDC_REPL_USER", "alpha_cdc_repl"),
        target_ddl_user=_text("CDC_TARGET_DDL_USER", "alpha_ddl"),
        target_sink_user=_text("CDC_TARGET_SINK_USER", "alpha_sink"),
        db_sslmode=_text("CDC_DB_SSLMODE", "require"),
        max_hold_ms=int(_number("CDC_MAX_HOLD_MS", 3000)),
        log_interval_s=_number("CDC_LOG_INTERVAL_S", 10),
        job_poll_s=_number("CDC_JOB_POLL_S", 3),
        probe_wait_s=_number("CDC_PROBE_WAIT_S", 180),
        probe_retry_s=_number("CDC_PROBE_RETRY_S", 10),
        # A live run removes the VPN itself after stop, which can take many minutes.
        complete_wait_s=_number("CDC_COMPLETE_WAIT_S", 1800),
        complete_poll_s=_number("CDC_COMPLETE_POLL_S", 5),
        cutover_wait_s=_number("CDC_CUTOVER_WAIT_S", 60),
        cutover_poll_s=_number("CDC_CUTOVER_POLL_S", 1),
        terraform_timeout_s=_number("CDC_TERRAFORM_TIMEOUT_S", 1800),
        tunnel_wait_s=_number("CDC_TUNNEL_WAIT_S", 600),
        tunnel_poll_s=_number("CDC_TUNNEL_POLL_S", 30),
        agent_timeout_s=_number("CDC_AGENT_TIMEOUT_S", 30),
        # Abort and close clean up before answering; Cloudflare waits about 100s at most.
        agent_abort_timeout_s=_number("CDC_AGENT_ABORT_TIMEOUT_S", 95),
    )
