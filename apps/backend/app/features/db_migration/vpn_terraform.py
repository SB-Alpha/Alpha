"""역할별 인증·작업 폴더·state를 분리한 Terraform 실행기. apply는 호출할 때만 한다."""

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from app.core.config import data_dir

from .progress_log import ProgressLog
from .settings import tunables
from .vpn import OciSettings, TERRAFORM_ENV, TERRAFORM_ENV_PREFIXES
from .vpn_plan import VpnError


class TerraformSession:
    def __init__(self, settings: OciSettings, job_id: str, roots: dict[str, str],
                 credentials: dict[str, Callable[[], dict[str, str]]], *, runner: Callable = subprocess.run,
                 log: ProgressLog | None = None):
        self.settings, self.job_id, self.roots = settings, job_id, roots
        self.credentials, self.runner, self.log = credentials, runner, log
        self.work_dir = data_dir() / "terraform" / job_id / "vpn-v2"
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ready: set[str] = set()

    def _env(self, role: str) -> dict[str, str]:
        try:
            specific = self.credentials[role]()
            if not isinstance(specific, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and v for k, v in specific.items()
            ):
                raise ValueError
        except Exception:
            raise VpnError("credentials", f"{role}: VPN credentials are unavailable or invalid") from None
        env = {k: v for k, v in os.environ.items() if k in TERRAFORM_ENV or k.startswith(TERRAFORM_ENV_PREFIXES)}
        env.update(specific)
        env.update(TF_IN_AUTOMATION="1", TF_INPUT="0", TF_DATA_DIR=str(self.work_dir / role / ".terraform"))
        return env

    def check_credentials(self) -> None:
        for role in self.roots:
            self._env(role)

    def _run(self, role: str, *args: str) -> str:
        root = self.roots[role]
        command = ["terraform", f"-chdir={self.settings.terraform_dir / root}", *args]
        try:
            done = self.runner(command, env=self._env(role), capture_output=True, text=True,
                               timeout=tunables().terraform_timeout_s)
        except VpnError:
            raise
        except Exception:
            raise VpnError("terraform", f"{role}: terraform {args[0]} could not run") from None
        if done.returncode:
            raise VpnError("terraform", f"{role}: terraform {args[0]} failed; saved VPN context is kept for cleanup")
        return done.stdout

    def init(self, role: str) -> None:
        if role in self._ready:
            return
        (self.work_dir / role).mkdir(parents=True, exist_ok=True, mode=0o700)
        s = self.settings
        self._run(role, "init", "-no-color", "-reconfigure", f"-backend-config=bucket={s.state_bucket}",
                  f"-backend-config=namespace={s.state_namespace}", f"-backend-config=key={self.job_id}/vpn-v2/{role}.tfstate",
                  f"-backend-config=region={s.region}", f"-backend-config=config_file_profile={s.profile}")
        self._ready.add(role)

    def _with_vars(self, role: str, action: tuple[str, ...], values: dict) -> None:
        self.init(role)
        fd, name = tempfile.mkstemp(prefix="vars-", suffix=".tfvars.json", dir=self.work_dir / role)
        path = Path(name)
        started = time.monotonic()
        self._event("terraform.start", role=role, action=" ".join(action))
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(values, stream)
            self._run(role, *action, "-auto-approve", "-no-color", f"-var-file={path}")
        except Exception:
            self._event("terraform.failed", role=role, action=" ".join(action), seconds=round(time.monotonic() - started))
            raise
        else:
            self._event("terraform.done", role=role, action=" ".join(action), seconds=round(time.monotonic() - started))
        finally:
            path.unlink(missing_ok=True)

    def _event(self, event: str, **fields: str | int) -> None:
        if self.log is not None:
            self.log.event(event, **fields)

    def apply(self, role: str, values: dict) -> None:
        self._with_vars(role, ("apply",), values)

    def refresh(self, role: str, values: dict) -> None:
        self._with_vars(role, ("apply", "-refresh-only"), values)

    def destroy(self, role: str, values: dict) -> None:
        self._with_vars(role, ("destroy",), values)

    def output(self, role: str, name: str):
        self.init(role)
        try:
            data = json.loads(self._run(role, "output", "-json", "-no-color") or "{}")
            if not isinstance(data, dict):
                raise ValueError
            return data.get(name, {}).get("value")
        except (ValueError, TypeError, AttributeError):
            raise VpnError("invalid_output", f"{role}: terraform returned invalid output") from None
