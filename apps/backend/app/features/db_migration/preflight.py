"""Checks before anything is built. Each one raises PreflightError and never quotes a secret value."""

import ipaddress
import logging
from collections.abc import Iterable

from .client import CdcAgentClient
from .schemas import DbMigrationInput, single_line
from .vpn import OciSettings

logger = logging.getLogger(__name__)

ENDED_STATES = {"aborted", "succeeded"}


class PreflightError(RuntimeError):
    pass


def check_agent(agent: CdcAgentClient) -> None:
    """Before the VPN: building it for 15-20 minutes on a broken cluster is wasted."""
    status = agent.status()
    problems = [name for name in ("kafka", "connect") if not status.get(name)]
    problems += status.get("plugins_missing") or []
    if problems:
        raise PreflightError(f"CDC cluster not ready: {', '.join(problems)}")


def check_app_account(mig: DbMigrationInput, db_user: str) -> None:
    """Cutover blocks writes with ALTER ROLE CURRENT_USER, so another account would leave the app writing."""
    if mig.source.app.db_user != db_user:
        raise PreflightError(
            "the given DB user is not the account the source app uses. Cutover makes the given account read-only "
            "to stop app writes, so give the app's account with admin rights. "
            "Separate admin and app accounts are not supported yet"
        )


def check_source(mig: DbMigrationInput, db_user: str) -> None:
    check_app_account(mig, db_user)
    if mig.options.skip_vpn:
        return
    # DB connections go through the VPN only; a public DB would resolve to a public address.
    if mig.source.db.publicly_accessible is True:
        raise PreflightError("the source DB has public access on; turn it off first")
    if mig.source.db.publicly_accessible is None:
        logger.warning("run %s: public access of the source DB is unknown", mig.run_id)


def _networks(field: str, values: Iterable[str]) -> list[tuple[str, ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    result = []
    for value in values:
        try:
            result.append((field, ipaddress.ip_network(value, strict=False)))
        except ValueError:
            raise PreflightError(f"{field} is not a CIDR block") from None
    return result


def check_networks(mig: DbMigrationInput, settings: OciSettings) -> None:
    """Routes between the three clouds cannot work when their ranges overlap."""
    if mig.options.skip_vpn:
        return
    source = _networks("source.network.cidr", [mig.source.network.cidr])
    target = _networks("target.network.subnet_cidrs", mig.target.network.subnet_cidrs)
    target += _networks("target.network.psa_range_cidr", [mig.target.network.psa_range_cidr])
    vcn = _networks("CDC_OCI_VCN_CIDR", [settings.vcn_cidr])
    node = _networks("CDC_NODE_SUBNET_CIDR", [settings.node_subnet_cidr])
    if not node[0][1].subnet_of(vcn[0][1]):
        raise PreflightError("CDC_NODE_SUBNET_CIDR is not inside CDC_OCI_VCN_CIDR")
    for left, right in ((source, target), (source, vcn), (target, vcn)):
        for left_name, left_net in left:
            for right_name, right_net in right:
                if left_net.overlaps(right_net):
                    raise PreflightError(f"address ranges overlap: {left_name} ({left_net}), {right_name} ({right_net})")


def check_job_secrets(values: dict[str, str]) -> None:
    """The agent refuses these only when they arrive, which is after the VPN is built. Names the keys only."""
    bad = sorted(key for key, value in values.items() if not single_line(value))
    if bad:
        raise PreflightError(f"must be a single line of text without surrounding spaces: {', '.join(bad)}")


def source_key(mig: DbMigrationInput) -> tuple[str, str]:
    return mig.source.db.host, mig.source.db.database


def check_not_running(mig: DbMigrationInput, job_id: str, agent: CdcAgentClient) -> None:
    """One slot, one cutover: two runs on the same source would fight over its writes."""
    key = source_key(mig)
    for job in agent.list_jobs():
        source = job["request"]["source"]
        if (job["job_id"] != job_id and job["state"] not in ENDED_STATES
                and (source["host"], source["database"]) == key):
            raise PreflightError(f"job {job['job_id']} already works on the same source DB ({job['state']})")
