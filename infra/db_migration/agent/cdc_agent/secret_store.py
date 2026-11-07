"""Job secrets shared by the 3 nodes.

A request reaches one agent, but Connect tasks read the properties file on whichever
node they run. So the agent writes the values encrypted to a compacted topic, and
every agent keeps its own files (mode 600) in step with that topic.

Each job has two files. Connect's folder holds only the two accounts its connectors use;
the admin, DDL and app accounts are in a folder that only the agent mounts.
"""

import logging
import os
import tempfile
from typing import Any

from confluent_kafka import Producer
from cryptography.fernet import Fernet, InvalidToken

from cdc_agent.config import Settings
from cdc_agent.models import JOB_ID_PATTERN
from cdc_agent.store import JobStore, read_topic

log = logging.getLogger("cdc_agent.secrets")

REQUIRED_KEYS = frozenset(
    f"{account}.{field}"
    for account in ("source", "source.app", "source.admin", "target", "target.ddl", "target.admin")
    for field in ("user", "password")
)
# Referenced by connectors/*.json.
CONNECT_KEYS = frozenset({"source.user", "source.password", "target.user", "target.password"})


class SecretsDisabled(RuntimeError):
    """SECRETS_KEY is not set on this node."""


def _escape(value: str) -> str:
    # Connect reads this as Java properties, where a backslash is an escape.
    return value.replace("\\", "\\\\")


def to_properties(values: dict[str, str]) -> bytes:
    lines = [f"{key}={_escape(value)}" for key, value in sorted(values.items())]
    return ("\n".join(lines) + "\n").encode()


def check_values(values: dict[str, Any]) -> None:
    """Errors name the keys only, never the values."""
    missing = REQUIRED_KEYS - values.keys()
    unknown = values.keys() - REQUIRED_KEYS
    if missing or unknown:
        raise ValueError(f"missing keys: {sorted(missing)}, unknown keys: {sorted(unknown)}")
    for key, value in values.items():
        if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"{key}: must be a single line of text without surrounding spaces")


class SecretStore:
    def __init__(self, settings: Settings, store: JobStore):
        self._settings = settings
        self._store = store
        self._fernet = Fernet(settings.secrets_key) if settings.secrets_key else None
        self._producer: Producer | None = None

    def ensure_topic(self) -> None:
        self._store.create_topics([self._settings.secrets_topic], compact=True)

    def put(self, job_id: str, values: dict[str, Any]) -> None:
        if self._fernet is None:
            raise SecretsDisabled("SECRETS_KEY is not set on this node")
        check_values(values)
        self._send(job_id, self._fernet.encrypt(to_properties(values)))
        self.sync()

    def delete(self, job_id: str) -> None:
        if self._fernet is not None:
            self._send(job_id, None)
        self._remove(job_id)

    def sync(self) -> None:
        if self._fernet is None:
            return
        for job_id, token in read_topic(self._settings, self._settings.secrets_topic).items():
            if not JOB_ID_PATTERN.match(job_id):
                log.error("ignored a secrets record with a bad key")
            elif token is None:
                self._remove(job_id)
            else:
                try:
                    self._write(job_id, self._fernet.decrypt(token))
                except InvalidToken:
                    log.error("secrets for %s do not decrypt with this node's SECRETS_KEY", job_id)

    def _paths(self, job_id: str):
        name = f"{job_id}.properties"
        return self._settings.secrets_dir / name, self._settings.agent_secrets_dir / name

    def _remove(self, job_id: str) -> None:
        for path in self._paths(job_id):
            path.unlink(missing_ok=True)

    def _write(self, job_id: str, content: bytes) -> None:
        lines = content.decode().splitlines()
        connect = [line for line in lines if line.split("=", 1)[0] in CONNECT_KEYS]
        agent = [line for line in lines if line.split("=", 1)[0] not in CONNECT_KEYS]
        connect_path, agent_path = self._paths(job_id)
        self._write_file(agent_path, ("\n".join(agent) + "\n").encode())
        # Replaces a file from before the split, so Connect's copy loses the other accounts.
        self._write_file(connect_path, ("\n".join(connect) + "\n").encode())

    @staticmethod
    def _write_file(path, content: bytes) -> None:
        if path.exists() and path.read_bytes() == content:
            return
        # mkstemp creates the file with mode 600; replace is atomic for a reader like Connect.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise

    def _send(self, job_id: str, value: bytes | None) -> None:
        if self._producer is None:
            self._producer = Producer({
                "bootstrap.servers": self._settings.kafka_bootstrap,
                "acks": "all",
                "enable.idempotence": True,
            })
        self._producer.produce(self._settings.secrets_topic, key=job_id, value=value)
        if self._producer.flush(10) != 0:
            raise RuntimeError("secrets were not acknowledged by Kafka")
