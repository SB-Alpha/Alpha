"""Agent settings from environment variables. No secret values live here."""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    node_id: str
    connect_url: str
    kafka_bootstrap: str
    state_topic: str
    state_replication_factor: int
    secrets_dir: Path
    connectors_dir: Path
    worker_url: str | None
    worker_token_file: Path | None
    inflight_timeout_s: float
    marker_timeout_s: float
    reconcile_interval_s: float
    secrets_key: str | None = field(default=None, repr=False)
    secrets_topic: str = "_alpha_cdc_secrets"
    # Connect reads only secrets_dir. Accounts it does not use stay here, mounted on the agent only.
    agent_secrets_dir: Path = Path("/etc/alpha-cdc/agent-secrets")
    worker_timeout_s: float = 5
    # Limit of the data comparison after the switch. Over it the result is unverifiable.
    verify_timeout_s: float = 3600
    # While waiting for approval, GET /jobs/{id} counts rows on both databases at most this often.
    metrics_interval_s: float = 30


@lru_cache
def get_settings() -> Settings:
    token_file = os.getenv("WORKER_TOKEN_FILE")
    return Settings(
        node_id=os.getenv("NODE_ID", "local"),
        connect_url=os.getenv("CONNECT_URL", "http://localhost:8083").rstrip("/"),
        kafka_bootstrap=os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"),
        state_topic=os.getenv("STATE_TOPIC", "_alpha_cdc_jobs"),
        state_replication_factor=int(os.getenv("KAFKA_REPLICATION_FACTOR", "1")),
        secrets_dir=Path(os.getenv("SECRETS_DIR", "/etc/alpha-cdc/secrets")),
        connectors_dir=Path(os.getenv("CONNECTORS_DIR", "/app/connectors")),
        worker_url=(os.getenv("WORKER_URL") or "").rstrip("/") or None,
        worker_token_file=Path(token_file) if token_file else None,
        inflight_timeout_s=float(os.getenv("INFLIGHT_TIMEOUT_S", "2")),
        marker_timeout_s=float(os.getenv("MARKER_TIMEOUT_S", "10")),
        reconcile_interval_s=float(os.getenv("RECONCILE_INTERVAL_S", "3")),
        secrets_key=os.getenv("SECRETS_KEY") or None,
        secrets_topic=os.getenv("SECRETS_TOPIC", "_alpha_cdc_secrets"),
        agent_secrets_dir=Path(os.getenv("AGENT_SECRETS_DIR", "/etc/alpha-cdc/agent-secrets")),
        worker_timeout_s=float(os.getenv("WORKER_TIMEOUT_S", "5")),
        verify_timeout_s=float(os.getenv("VERIFY_TIMEOUT_S", "3600")),
        metrics_interval_s=float(os.getenv("METRICS_INTERVAL_S", "30")),
    )


def read_properties(path: Path) -> dict[str, str]:
    """Java .properties subset: key=value per line, # comments. Same file Connect reads."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Connect reads this file as Java properties, where a backslash is an escape.
        values[key.strip()] = value.strip().replace("\\\\", "\\")
    return values
