"""Request shapes for the CDC agent and the run input. The agent validates the details (names, tables)."""

from typing import Any

from pydantic import BaseModel, Field


class SourceSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    sslmode: str = "require"
    tables: list[str] = Field(min_length=1, description="schema.table, lowercase")
    app_role: str = Field(description="Role the source app writes with; blocked at cutover")


class TargetSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    sslmode: str = "require"
    app_role: str | None = Field(default=None, description="Role the target app uses; gets DML on created tables")


class JobOptions(BaseModel):
    max_hold_ms: int = 3000


class JobRequest(BaseModel):
    job_id: str = Field(description="lowercase letters, digits, underscore; 2-31 chars")
    source: SourceSpec
    target: TargetSpec
    options: JobOptions = JobOptions()


class CutoverRequest(BaseModel):
    approved_by: str = Field(min_length=1)


# Run input: source from assessment, target from provisioning.

class SourceDb(BaseModel):
    instance_id: str | None = None
    host: str
    port: int = 5432
    database: str
    version: str | None = None
    publicly_accessible: bool | None = None
    logical_replication: bool | None = None


class SourceNetwork(BaseModel):
    vpc_id: str
    cidr: str
    route_table_ids: list[str] = Field(min_length=1)
    db_security_group_id: str | None = None


class SourceApp(BaseModel):
    db_user: str


class SourceCredentials(BaseModel):
    cloud: str
    db_admin: str


class SourceInput(BaseModel):
    cloud: str = "aws"
    account_id: str
    region: str
    db: SourceDb
    network: SourceNetwork
    app: SourceApp
    tables: list[str] = Field(min_length=1)
    credentials_ref: SourceCredentials


class TargetDb(BaseModel):
    instance: str
    private_ip: str
    port: int = 5432
    database: str
    version: str | None = None
    ssl_mode: str | None = None


class TargetNetwork(BaseModel):
    vpc_name: str
    subnet_cidrs: list[str] = Field(min_length=1)
    psa_range_cidr: str


class TargetApp(BaseModel):
    url: str | None = None
    db_user: str


class TargetCredentials(BaseModel):
    cloud: str
    admin: str
    # Left out: the module makes the password itself and keeps it for the run.
    ddl_user: str | None = None
    sink_user: str | None = None


class TargetInput(BaseModel):
    cloud: str = "gcp"
    project_id: str
    region: str
    db: TargetDb
    network: TargetNetwork
    app: TargetApp
    credentials_ref: TargetCredentials


class MigrationOptions(BaseModel):
    log_interval_s: float = 10
    max_hold_ms: int = 3000
    skip_vpn: bool = Field(default=False, description="For local tests: source and target are already reachable")
    # Used from the second phase (cutover).
    approval_timeout_s: float = 86400
    stabilize_s: float = 300


class DbMigrationInput(BaseModel):
    run_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    source: SourceInput
    target: TargetInput
    options: MigrationOptions = MigrationOptions()


class DbMigrationResult(BaseModel):
    run_id: str
    job_id: str
    state: str
    message: str | None = None
    outputs: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    vpn: dict[str, Any] = {}
