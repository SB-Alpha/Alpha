"""Request shapes for the CDC agent and the run input. The agent validates the details (names, tables)."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .settings import tunables

RUN_ID_PATTERN = r"^[a-z][a-z0-9-]{1,30}$"
CutoverOutcome = Literal["not_started", "rolled_back", "switched", "post_switch_failed", "unknown"]


def single_line(value: str) -> bool:
    """The form the CDC agent accepts for a secret value."""
    return bool(value) and value == value.strip() and "\n" not in value and "\r" not in value


def _sslmode() -> str:
    return tunables().db_sslmode


def _max_hold_ms() -> int:
    return tunables().max_hold_ms


def _log_interval_s() -> float:
    return tunables().log_interval_s


class SourceSpec(BaseModel):
    host: str
    port: int = 5432  # PostgreSQL's own default
    database: str
    sslmode: str = Field(default_factory=_sslmode)
    tables: list[str] = Field(min_length=1, description="schema.table, lowercase")
    app_role: str = Field(description="Role the source app writes with; blocked at cutover")


class TargetSpec(BaseModel):
    host: str
    port: int = 5432
    database: str
    sslmode: str = Field(default_factory=_sslmode)
    app_role: str | None = Field(default=None, description="Role the target app uses; gets DML on created tables")
    keep_ddl_user: bool = Field(default=False, description="The caller's own account; keeps its login after the job")
    keep_sink_user: bool = Field(default=False, description="The caller's own account; keeps its login after the job")


class JobOptions(BaseModel):
    max_hold_ms: int = Field(default_factory=_max_hold_ms)


class JobRequest(BaseModel):
    job_id: str = Field(description="lowercase letters, digits, underscore; 2-31 chars")
    source: SourceSpec
    target: TargetSpec
    options: JobOptions = Field(default_factory=JobOptions)


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
    # Required unless options.pass_onprem; see require_gcp_fields.
    vpc_name: str | None = None
    subnet_cidrs: list[str] | None = Field(default=None, min_length=1)
    psa_range_cidr: str | None = None


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
    project_id: str | None = None  # required unless options.pass_onprem
    region: str
    db: TargetDb
    network: TargetNetwork = Field(default_factory=TargetNetwork)
    app: TargetApp
    credentials_ref: TargetCredentials


class MigrationOptions(BaseModel):
    log_interval_s: float = Field(default_factory=_log_interval_s)
    max_hold_ms: int = Field(default_factory=_max_hold_ms)
    skip_vpn: bool = Field(default=False, description="For local tests: source and target are already reachable")
    pass_onprem: bool = Field(default=False, description="목적지가 온프렘(Cloudflare 터널)이라 OCI<->GCP 구간을 만들지 않고 AWS<->OCI만 연결")


def require_gcp_fields(target: TargetInput, options: "MigrationOptions") -> None:
    """The GCP fields stay mandatory unless the GCP leg is skipped."""
    if options.pass_onprem:
        return
    n = target.network
    missing = [name for name, v in (("project_id", target.project_id), ("network.vpc_name", n.vpc_name),
                                    ("network.subnet_cidrs", n.subnet_cidrs),
                                    ("network.psa_range_cidr", n.psa_range_cidr)) if not v]
    if missing:
        raise ValueError(f"target.{', target.'.join(missing)} required unless options.pass_onprem")


class DbMigrationInput(BaseModel):
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    source: SourceInput
    target: TargetInput
    options: MigrationOptions = Field(default_factory=MigrationOptions)

    @model_validator(mode="after")
    def _gcp_fields(self) -> "DbMigrationInput":
        require_gcp_fields(self.target, self.options)
        return self


# [이전 입력 앞단] run_input.py로 대체했다. 조회 단계 연결이 끝나면 지운다.
# class StartInput(BaseModel):
#     """What the user gives. The four secret-like values are kept out of repr and logs."""
#
#     region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d+$", description="AWS region of the source")
#     aws_access_key_id: SecretStr = Field(min_length=1)
#     aws_secret_access_key: SecretStr = Field(min_length=1)
#     aws_session_token: SecretStr | None = None
#     db_user: SecretStr = Field(min_length=1)
#     db_password: SecretStr = Field(min_length=1)
#     target: TargetInput
#     run_id: str | None = Field(default=None, pattern=RUN_ID_PATTERN)
#     source: SourceInput | None = Field(default=None, description="Left out: found with the lookup the caller passes")
#     options: MigrationOptions = Field(default_factory=MigrationOptions)
#
#     @model_validator(mode="after")
#     def _gcp_fields(self) -> "StartInput":
#         require_gcp_fields(self.target, self.options)
#         return self
#
#     @field_validator("db_user", "db_password")
#     @classmethod
#     def check_single_line(cls, value: SecretStr) -> SecretStr:
#         # The message must not quote the value.
#         if not single_line(value.get_secret_value()):
#             raise ValueError("must be a single line of text without surrounding spaces")
#         return value


class SourceLookup(BaseModel):
    """What a source lookup gets. References only."""

    run_id: str
    region: str
    target_region: str
    cloud_ref: str
    db_admin_ref: str


class RunStatus(BaseModel):
    run_id: str
    job_id: str
    state: str
    phase: str
    progress: int
    message: str | None = None
    metrics: dict[str, Any] = {}
    ready_for_cutover: bool = False
    cleanup_required: bool = False
    remaining: list[str] = []


class CutoverResult(BaseModel):
    run_id: str
    job_id: str
    outcome: CutoverOutcome
    state: str
    phase: str
    message: str
    detail: str | None = None
    hold_ms: float | None = None
    metrics: dict[str, Any] = {}
    evidence: dict[str, Any] = {}


class FinishResult(BaseModel):
    run_id: str
    job_id: str
    state: str = Field(description="finished or incomplete; call again when incomplete")
    message: str | None = None
    cleanup: dict[str, Any] = {}
    target_accounts: dict[str, Any] = {}
    vpn: str = Field(description="removed or kept")
    remaining: list[str] = []
    next_steps: list[str] = []


class DbMigrationResult(BaseModel):
    run_id: str
    job_id: str
    state: str
    message: str | None = None
    outputs: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    vpn: dict[str, Any] = {}
