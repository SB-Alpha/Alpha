"""Progress log: one JSON line per event.

Never pass secret values here; references and hosts are fine.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app.features.db_migration")


class ProgressLog:
    def __init__(self, run_id: str, job_id: str):
        self.run_id = run_id
        self.job_id = job_id

    def event(self, event: str, **fields: Any) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "run_id": self.run_id,
            "job_id": self.job_id,
            **fields,
        }
        logger.info(json.dumps(line, ensure_ascii=False, default=str))
