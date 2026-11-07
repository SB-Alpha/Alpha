"""VPN 공개 입구. VpnPlan은 역할 기반 공통 절차(vpn_workflow.py)로 넘긴다.

아래 구현은 기존 DbMigrationInput과 기존 state 정리를 위한 AWS→GCP 호환 경로다.

Each side needs the other's public IP, so the roots are applied in this order:
  a. gcp      no tunnels yet                → GCP HA VPN IPs
  b. aws-vpn  placeholder customer gateways → AWS tunnel IPs
  c. oci-vpn  AWS and GCP IPs               → OCI tunnel IPs
  d. aws-vpn  real customer gateways (the VPN connections stay, per Oracle's AWS guide)
  e. gcp      tunnels to OCI
  f. wait until the OCI tunnels are UP

Cloud keys reach Terraform only through environment variables. Tunnel secrets go through
a tfvars file with owner-only access that is removed right after each command.
"""

import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import data_dir
from app.core.secrets import put_secret, resolve

from .schemas import DbMigrationInput
from .settings import required, tunables
from .vpn_plan import VpnPlan, VpnError

GCP, AWS, OCI = "gcp", "aws-vpn", "oci-vpn"
# Must match the defaults of the Terraform modules, which take no input for them.
AWS_ASN, GCP_ASN = "64512", "65010"
# Any valid address works until the real one is known; each connection needs its own gateway.
PLACEHOLDER_IPS = ["1.1.1.1", "1.1.1.2"]
# VPNs saved before the context kept db_port were built with this port.
OLD_CONTEXT_DB_PORT = 5432
# OCI refuses 169.254.10.0-169.254.19.255. AWS takes .1 and OCI takes .2; GCP takes .1 and OCI takes .2.
BGP_NETS = {"aws-0": "169.254.21", "aws-1": "169.254.22", "gcp-0": "169.254.31", "gcp-1": "169.254.32"}
TUNNEL_KEYS = tuple(BGP_NETS)


def active_keys(pass_onprem: bool) -> tuple[str, ...]:
    return tuple(k for k in TUNNEL_KEYS if not (pass_onprem and k.startswith("gcp")))


# All Terraform gets from this process's environment. App secrets and local cloud logins stay out.
# OCI_ and the OCI profile are its own login: the OCI side has no key saved per run.
TERRAFORM_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TEMP", "TMP", "TZ",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "TF_CLI_CONFIG_FILE",
})
TERRAFORM_ENV_PREFIXES = ("LC_", "OCI_")

Runner = Callable[..., subprocess.CompletedProcess]


class TerraformError(RuntimeError):
    pass


@dataclass(frozen=True)
class OciSettings:
    terraform_dir: Path
    region: str
    profile: str
    compartment_id: str
    drg_id: str
    state_bucket: str
    state_namespace: str
    vcn_cidr: str
    node_subnet_cidr: str

    @classmethod
    def from_env(cls) -> "OciSettings":
        default_dir = Path(__file__).resolve().parents[5] / "infra" / "db_migration" / "terraform"
        return cls(
            terraform_dir=Path(os.getenv("CDC_TERRAFORM_DIR", default_dir)),
            region=required("CDC_OCI_REGION"),
            # The name the OCI tools themselves use when none is given.
            profile=os.getenv("CDC_OCI_PROFILE", "DEFAULT"),
            compartment_id=required("CDC_OCI_COMPARTMENT_ID"),
            drg_id=required("CDC_OCI_DRG_ID"),
            state_bucket=required("CDC_TFSTATE_BUCKET"),
            state_namespace=required("CDC_TFSTATE_NAMESPACE"),
            vcn_cidr=required("CDC_OCI_VCN_CIDR"),
            node_subnet_cidr=required("CDC_NODE_SUBNET_CIDR"),
        )


class Terraform:
    """Terraform commands for one run. Each root has its own data folder and state object."""

    def __init__(self, settings: OciSettings, job_id: str, source_cloud: dict, target_cloud: dict | None,
                 *, runner: Runner = subprocess.run):
        self.settings = settings
        self.job_id = job_id
        self.runner = runner
        self.work_dir = data_dir() / "terraform" / job_id
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.plugin_cache = data_dir() / "terraform" / "plugins"
        self.plugin_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        aws_env = {
            "AWS_ACCESS_KEY_ID": source_cloud["aws_access_key_id"],
            "AWS_SECRET_ACCESS_KEY": source_cloud["aws_secret_access_key"],
        }
        if source_cloud.get("aws_session_token"):  # temporary credentials
            aws_env["AWS_SESSION_TOKEN"] = source_cloud["aws_session_token"]
        self.cloud_env = {
            AWS: aws_env,
            OCI: {},
        }
        if target_cloud:  # None when the GCP leg is skipped
            self.cloud_env[GCP] = {"GOOGLE_CREDENTIALS": target_cloud["credentials_json"]}
        self._ready: set[str] = set()

    def _env(self, root: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in TERRAFORM_ENV or k.startswith(TERRAFORM_ENV_PREFIXES)}
        env.update(self.cloud_env[root])
        env.update(TF_IN_AUTOMATION="1", TF_INPUT="0", TF_DATA_DIR=str(self.work_dir / root / ".terraform"),
                   TF_PLUGIN_CACHE_DIR=str(self.plugin_cache))
        return env

    def _run(self, root: str, *args: str) -> str:
        command = ["terraform", f"-chdir={self.settings.terraform_dir / root}", *args]
        done = self.runner(command, env=self._env(root), capture_output=True, text=True, timeout=tunables().terraform_timeout_s)
        if done.returncode != 0:
            raise TerraformError(f"terraform {args[0]} failed in {root}: {done.stderr.strip()[-800:]}")
        return done.stdout

    def init(self, root: str) -> None:
        if root in self._ready:
            return
        (self.work_dir / root).mkdir(parents=True, exist_ok=True, mode=0o700)
        s = self.settings
        self._run(root, "init", "-no-color", "-reconfigure",
                  f"-backend-config=bucket={s.state_bucket}",
                  f"-backend-config=namespace={s.state_namespace}",
                  f"-backend-config=key={self.job_id}/{root}.tfstate",
                  f"-backend-config=region={s.region}",
                  f"-backend-config=config_file_profile={s.profile}")
        self._ready.add(root)

    def _with_vars(self, root: str, command: tuple[str, ...], tfvars: dict[str, Any]) -> None:
        self.init(root)
        path = self.work_dir / root / "run.tfvars.json"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tfvars, f)
        try:
            self._run(root, *command, "-no-color", f"-var-file={path}")
        finally:
            path.unlink(missing_ok=True)

    def apply(self, root: str, tfvars: dict[str, Any]) -> None:
        self._with_vars(root, ("apply", "-auto-approve"), tfvars)

    def refresh(self, root: str, tfvars: dict[str, Any]) -> None:
        self._with_vars(root, ("apply", "-refresh-only", "-auto-approve"), tfvars)

    def destroy(self, root: str, tfvars: dict[str, Any]) -> None:
        self._with_vars(root, ("destroy", "-auto-approve"), tfvars)

    def output(self, root: str, name: str) -> Any:
        """The value, or None when the root has no such output yet (nothing applied)."""
        self.init(root)
        outputs = json.loads(self._run(root, "output", "-json", "-no-color") or "{}")
        return outputs.get(name, {}).get("value")


@dataclass(frozen=True)
class VpnContext:
    """What destroy needs later, when only run_id is known. References only, no values."""

    run_id: str
    job_id: str
    name: str
    source: dict
    target: dict
    pass_onprem: bool = False  # on-prem destination (Cloudflare tunnel): AWS<->OCI only, no OCI<->GCP leg

    @classmethod
    def from_input(cls, mig: DbMigrationInput, job_id: str) -> "VpnContext":
        pass_onprem = mig.options.pass_onprem
        return cls(
            run_id=mig.run_id,
            pass_onprem=pass_onprem,
            job_id=job_id,
            name="cdc-" + job_id.replace("_", "-"),
            source={
                "region": mig.source.region,
                "vpc_id": mig.source.network.vpc_id,
                "cidr": mig.source.network.cidr,
                "route_table_ids": mig.source.network.route_table_ids,
                "db_security_group_id": mig.source.network.db_security_group_id,
                "db_port": mig.source.db.port,
                "cloud_ref": mig.source.credentials_ref.cloud,
            },
            target={} if pass_onprem else {
                "project_id": mig.target.project_id,
                "region": mig.target.region,
                "vpc_name": mig.target.network.vpc_name,
                "subnet_cidrs": mig.target.network.subnet_cidrs,
                "psa_range_cidr": mig.target.network.psa_range_cidr,
                "cloud_ref": mig.target.credentials_ref.cloud,
            },
        )

    def save(self) -> None:
        put_secret(f"{self.run_id}/vpn-context", {"json": json.dumps(self.__dict__)})

    @classmethod
    def load(cls, run_id: str) -> "VpnContext":
        data = json.loads(resolve(f"secret://{run_id}/vpn-context")["json"])
        if data.get("version") == 2:
            raise VpnError("context_version", "this run uses the role-based VPN flow; legacy input must not overwrite it")
        return cls(**{**data, "source": {"db_port": OLD_CONTEXT_DB_PORT, **data["source"]}})


def ensure_psk(run_id: str) -> dict[str, str]:
    """Tunnel secrets, made once per run. Hex only: OCI refuses some symbols."""
    ref = f"secret://{run_id}/vpn-psk"
    try:
        return resolve(ref)
    except KeyError:
        value = {key: secrets.token_hex(24) for key in TUNNEL_KEYS}
        put_secret(ref, value)
        return value


def _gcp_vars(ctx: VpnContext, psk: dict, oci_ips: dict) -> dict:
    t = ctx.target
    linked = all(k in oci_ips for k in ("gcp-0", "gcp-1"))
    return {
        "project": t["project_id"],
        "region": t["region"],
        "network": t["vpc_name"],
        "name": ctx.name,
        "advertised_ip_ranges": [t["psa_range_cidr"]],
        "oci_tunnels": [
            {"oci_ip": oci_ips[k], "gcp_bgp_ip": f"{BGP_NETS[k]}.1/30", "oci_bgp_ip": f"{BGP_NETS[k]}.2"}
            for k in ("gcp-0", "gcp-1")
        ] if linked else [],
        "shared_secrets": [psk["gcp-0"], psk["gcp-1"]] if linked else [],
    }


def _aws_vars(ctx: VpnContext, settings: OciSettings, psk: dict, oci_ips: dict) -> dict:
    s = ctx.source
    return {
        "region": s["region"],
        "name": ctx.name,
        "vpc_id": s["vpc_id"],
        "route_table_ids": s["route_table_ids"],
        "db_security_group_id": s["db_security_group_id"],
        "db_port": s["db_port"],
        # Only the CDC nodes reach the DB, not the rest of the VCN.
        "oci_cidr": settings.node_subnet_cidr,
        "tunnels": [
            {"oci_ip": oci_ips.get(k, PLACEHOLDER_IPS[i]), "inside_cidr": f"{BGP_NETS[k]}.0/30"}
            for i, k in enumerate(("aws-0", "aws-1"))
        ],
        "shared_secrets": [psk["aws-0"], psk["aws-1"]],
    }


def _oci_vars(ctx: VpnContext, settings: OciSettings, psk: dict, aws_ips: list, gcp_ips: list) -> dict:

    def tunnel(key: str, cpe_ip: str, cidrs: list[str], asn: str) -> dict:
        return {"cpe_ip": cpe_ip, "peer_cidrs": cidrs, "peer_asn": asn,
                "oracle_bgp_ip": f"{BGP_NETS[key]}.2/30", "peer_bgp_ip": f"{BGP_NETS[key]}.1/30"}

    tunnels = {
        "aws-0": tunnel("aws-0", aws_ips[0], [ctx.source["cidr"]], AWS_ASN),
        "aws-1": tunnel("aws-1", aws_ips[1], [ctx.source["cidr"]], AWS_ASN),
    }
    if not ctx.pass_onprem:
        gcp_cidrs = [*ctx.target["subnet_cidrs"], ctx.target["psa_range_cidr"]]
        tunnels["gcp-0"] = tunnel("gcp-0", gcp_ips[0], gcp_cidrs, GCP_ASN)
        tunnels["gcp-1"] = tunnel("gcp-1", gcp_ips[1], gcp_cidrs, GCP_ASN)
    return {
        "region": settings.region,
        "config_profile": settings.profile,
        "compartment_id": settings.compartment_id,
        "name": ctx.name,
        "drg_id": settings.drg_id,
        "tunnels": tunnels,
        "shared_secrets": {key: psk[key] for key in tunnels},
    }


def _aws_ips(tf: Terraform) -> list[str]:
    return [t["aws_ip"] for t in tf.output(AWS, "tunnels") or []]


def _saved_context(run_id: str) -> VpnContext | None:
    try:
        return VpnContext.load(run_id)
    except KeyError:
        return None


def _tunnels_up(status: dict, keys: tuple[str, ...] = TUNNEL_KEYS) -> bool:
    return set(status) == set(keys) and all(v == "UP" for v in status.values())


def _existing_tunnels(tf: Terraform, ctx: VpnContext, saved: VpnContext | None, settings: OciSettings,
                      psk: dict) -> dict | None:
    """Live tunnel status when this run's VPN can be reused, otherwise None.

    A rerun with the same input skips the five applies (15-20 minutes). OCI's live status
    decides because the saved state may be old and a placeholder gateway never comes UP.
    """
    if saved != ctx:
        return None
    oci_ips, aws_ips = tf.output(OCI, "oracle_tunnel_ips"), _aws_ips(tf)
    gcp_ips = [] if ctx.pass_onprem else tf.output(GCP, "ha_vpn_interface_ips")
    if not (oci_ips and (gcp_ips or ctx.pass_onprem) and aws_ips):
        return None
    tf.refresh(OCI, _oci_vars(ctx, settings, psk, aws_ips, gcp_ips))
    status = tf.output(OCI, "tunnel_status") or {}
    return status if _tunnels_up(status, active_keys(ctx.pass_onprem)) else None


def ensure_vpn(mig: DbMigrationInput | VpnPlan, job_id: str, *, terraform: Terraform | None = None,
               sleep: Callable[[float], None] = time.sleep) -> dict:
    """Safe to call again: a healthy VPN is reused as it is; otherwise Terraform leaves what exists."""
    if isinstance(mig, VpnPlan):
        from .vpn_workflow import ensure_vpn as ensure_role_vpn

        if job_id != mig.job_id:
            raise VpnError("invalid_plan", "VPN job ID does not match the plan")
        return ensure_role_vpn(mig, terraform=terraform, sleep=sleep)
    ctx = VpnContext.from_input(mig, job_id)
    saved = _saved_context(mig.run_id)
    ctx.save()
    psk = ensure_psk(mig.run_id)
    settings = OciSettings.from_env()
    tf = terraform or Terraform(settings, job_id, resolve(ctx.source["cloud_ref"]),
                                None if ctx.pass_onprem else resolve(ctx.target["cloud_ref"]))

    existing = _existing_tunnels(tf, ctx, saved, settings, psk)
    if existing:
        return {"name": ctx.name, "tunnel_status": existing, "skipped": True}

    oci_ips = tf.output(OCI, "oracle_tunnel_ips") or {}
    gcp_ips = []
    if not ctx.pass_onprem:
        tf.apply(GCP, _gcp_vars(ctx, psk, oci_ips))
        gcp_ips = tf.output(GCP, "ha_vpn_interface_ips")
    tf.apply(AWS, _aws_vars(ctx, settings, psk, oci_ips))
    aws_ips = _aws_ips(tf)

    oci_vars = _oci_vars(ctx, settings, psk, aws_ips, gcp_ips)
    tf.apply(OCI, oci_vars)
    oci_ips = tf.output(OCI, "oracle_tunnel_ips")

    tf.apply(AWS, _aws_vars(ctx, settings, psk, oci_ips))
    if _aws_ips(tf) != aws_ips:
        # The OCI side was built for the old addresses and its CPE cannot be edited in place.
        raise RuntimeError("AWS tunnel IPs changed when the customer gateways were replaced")
    if not ctx.pass_onprem:
        tf.apply(GCP, _gcp_vars(ctx, psk, oci_ips))

    return {"name": ctx.name, "tunnel_status": _wait_tunnels(tf, oci_vars, sleep), "skipped": False}


def _wait_tunnels(tf: Terraform, oci_vars: dict, sleep: Callable[[float], None]) -> dict:
    keys = tuple(oci_vars["tunnels"])
    settings = tunables()
    deadline = time.monotonic() + settings.tunnel_wait_s
    while True:
        tf.refresh(OCI, oci_vars)
        status = tf.output(OCI, "tunnel_status") or {}
        if _tunnels_up(status, keys):
            return status
        if time.monotonic() > deadline:
            raise RuntimeError(f"VPN tunnels not up after {settings.tunnel_wait_s}s: {status}")
        sleep(settings.tunnel_poll_s)


def destroy_vpn(run_id: str, *, terraform: Terraform | None = None) -> bool:
    """Remove the VPN of a run. False when this run never made one. Safe to call again."""
    try:
        version = json.loads(resolve(f"secret://{run_id}/vpn-context")["json"]).get("version")
    except KeyError:
        return False
    if version == 2:
        from .vpn_workflow import destroy_vpn as destroy_role_vpn

        return destroy_role_vpn(run_id, terraform=terraform)
    try:
        ctx = VpnContext.load(run_id)
    except KeyError:
        return False
    psk = ensure_psk(run_id)
    settings = OciSettings.from_env()
    tf = terraform or Terraform(settings, ctx.job_id, resolve(ctx.source["cloud_ref"]),
                                None if ctx.pass_onprem else resolve(ctx.target["cloud_ref"]))

    oci_ips = tf.output(OCI, "oracle_tunnel_ips") or {}
    aws_ips = _aws_ips(tf) or PLACEHOLDER_IPS
    gcp_ips = PLACEHOLDER_IPS if ctx.pass_onprem else tf.output(GCP, "ha_vpn_interface_ips") or PLACEHOLDER_IPS

    # Keep going after a failure: leftovers cost money.
    errors = []
    roots = [(AWS, _aws_vars(ctx, settings, psk, oci_ips)), (OCI, _oci_vars(ctx, settings, psk, aws_ips, gcp_ips))]
    if not ctx.pass_onprem:
        roots.insert(0, (GCP, _gcp_vars(ctx, psk, oci_ips)))
    for root, tfvars in roots:
        try:
            tf.destroy(root, tfvars)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise TerraformError("; ".join(errors))
    return True
