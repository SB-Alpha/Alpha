"""Job request and state shapes."""

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

State = Literal["pending", "running", "waiting_approval", "paused", "succeeded", "failed", "aborted"]
Phase = Literal[
    "source_prepare",
    "target_prepare",
    "precheck",
    "replication_setup",
    "snapshot",
    "streaming",
    "cutover_ready",
    "cutover",
    "stabilizing",
    "closed",
]
TERMINAL_STATES = {"succeeded", "failed", "aborted"}

# Topic prefix, slot and publication names are built from job_id, so keep it simple.
JOB_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TABLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

MARKER_TABLE = "alpha_cdc.cutover_marker"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SourceSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    sslmode: str = "require"
    tables: list[str] = Field(min_length=1)
    app_role: str = Field(description="Role the source app writes with; blocked at cutover")

    @field_validator("tables")
    @classmethod
    def check_tables(cls, tables: list[str]) -> list[str]:
        for table in tables:
            if not TABLE_PATTERN.match(table):
                raise ValueError(f"table must be schema.table in lowercase: {table}")
        if MARKER_TABLE in tables:
            raise ValueError("marker table is added automatically")
        return tables


class TargetSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    sslmode: str = "require"
    app_role: str | None = Field(default=None, description="Role the target app uses; gets DML on created tables")
    keep_ddl_user: bool = Field(default=False, description="The caller's own account; keeps its login after the job")
    keep_sink_user: bool = Field(default=False, description="The caller's own account; keeps its login after the job")


class JobOptions(BaseModel):
    max_hold_ms: int = 3000


class JobRequest(BaseModel):
    job_id: str
    source: SourceSpec
    target: TargetSpec
    options: JobOptions = JobOptions()

    @field_validator("job_id")
    @classmethod
    def check_job_id(cls, job_id: str) -> str:
        if not JOB_ID_PATTERN.match(job_id):
            raise ValueError("job_id: lowercase letters, digits, underscore; 2-31 chars")
        return job_id


class Job(BaseModel):
    job_id: str
    request: JobRequest
    state: State = "pending"
    phase: Phase = "source_prepare"
    progress: int = 0
    message: str = ""
    metrics: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    @property
    def source_connector(self) -> str:
        return f"{self.job_id}-source"

    @property
    def sink_connector(self) -> str:
        return f"{self.job_id}-sink"

    @property
    def slot_name(self) -> str:
        return f"alpha_cdc_{self.job_id}"

    @property
    def publication_name(self) -> str:
        return f"alpha_cdc_{self.job_id}"

    @property
    def all_tables(self) -> list[str]:
        return [*self.request.source.tables, MARKER_TABLE]

    def report(self, *, state: State | None = None, phase: Phase | None = None,
               progress: int | None = None, message: str | None = None) -> None:
        if state is not None:
            self.state = state
        if phase is not None:
            self.phase = phase
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        self.updated_at = now()


class CutoverRequest(BaseModel):
    approved_by: str = Field(min_length=1, description="Who approved the write handover")
