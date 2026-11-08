"""Secret 원문 없는 버전별 상태와 프로세스 간 배포 잠금."""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.features.provisioning.terraform import ProvisioningError


def fingerprint(config):
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


class State:
    def __init__(self, config):
        self.root = config.directory
        self.path = self.root / "state.json"
        self.config_hash = fingerprint(config)

    @contextmanager
    def lock(self):
        if self.root.is_symlink():
            raise ProvisioningError("배포 디렉터리에 심볼릭 링크를 사용할 수 없습니다")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.root / ".deployment.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProvisioningError(
                    "동일 온프렘 목적지에서 다른 배포가 실행 중입니다"
                ) from None
            try:
                owner = self.root / ".owner.json"
                if owner.exists():
                    if (
                        json.loads(owner.read_text()).get("config_hash")
                        != self.config_hash
                    ):
                        raise ProvisioningError(
                            "목적지 소유 설정과 다릅니다. 기존 배포 인수를 거부합니다"
                        )
                else:
                    if any(p.name != ".deployment.lock" for p in self.root.iterdir()):
                        raise ProvisioningError(
                            "비어 있지 않은 기존 디렉터리를 자동 인수하지 않습니다"
                        )
                    atomic_write(owner, json.dumps({"config_hash": self.config_hash}))
                self.root.chmod(0o700)
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def load(self):
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        if data.get("version") != 1 or data.get("config_hash") != self.config_hash:
            raise ProvisioningError(
                "저장된 목적지 설정과 다릅니다. 기존 DB·Secret의 자동 교체를 거부합니다"
            )
        return data

    def save(self, data):
        payload = {**data, "version": 1, "config_hash": self.config_hash}
        atomic_write(self.path, json.dumps(payload, indent=2))


def atomic_write(path: Path, text: str):
    if path.is_symlink():
        raise ProvisioningError("심볼릭 링크 파일에 배포 설정을 쓸 수 없습니다")
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
