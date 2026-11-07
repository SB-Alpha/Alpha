"""Request shapes for the CDC agent and the run input. The agent validates the details (names, tables)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypedDict

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


class RouteEnd(BaseModel):
    cloud: str
    region: str


class Route(BaseModel):
    source: RouteEnd
    target: RouteEnd


# 에이전트가 정하는 값은 새 값이 와도 상태 조회가 깨지지 않게 문자열로 받고, 허용값은 API 문서에 싣는다.
RUN_STATES = ("pending", "running", "waiting_approval", "paused", "succeeded", "failed", "aborted", "stopped", "unknown")
AGENT_PHASES = ("source_prepare", "target_prepare", "precheck", "replication_setup", "snapshot", "streaming",
                "cutover_ready", "cutover", "stabilizing", "closed")
RUN_PHASES = ("inputs", "vpn", "probe", "secrets", "job", *AGENT_PHASES, "unknown")
LAST_STEP = 12


class RunError(BaseModel):
    """화면은 code로 분기하고 message는 그대로 보여 준다. 비밀값과 응답 원문은 담지 않는다."""

    code: str
    message: str


class DatabaseChoices(BaseModel):
    """이 값 중에서 골라 POST /runs/{run_id}/retry로 보낸다."""

    source: list[str]
    target: list[str]


class TableProgress(TypedDict, total=False):
    source_rows: int
    target_rows: int
    percent: float
    match: bool


class CutoverStep(TypedDict, total=False):
    step: str
    elapsed_ms: float
    waited_ms: float
    mismatch: int
    error: str


class RunMetrics(TypedDict, total=False):
    """없는 값은 보내지 않는다. 화면은 없는 값을 미제공으로 표시한다."""

    __pydantic_config__ = ConfigDict(extra="allow")

    phase: str
    simulated: bool
    copy_percent: float
    tables: dict[str, TableProgress]
    rows_checked: int
    mismatch: int
    measured_at: str
    measure_error: str
    replication_lag_bytes: int
    verification: Literal["running", "match", "mismatch", "unverifiable", "unknown"]
    cutover_seconds: float
    cutover_seconds_source: Literal["worker", "agent"]
    cutover_steps: list[CutoverStep]
    cutover_sequence: Literal["VERIFIED", "INCOMPLETE"]
    cutover_sequence_missing: list[str]
    cutover_outcome: CutoverOutcome
    serving: Literal["source", "target"]
    target_db_network: Literal["PRIVATE", "PUBLIC"]
    cdc_error: str


class MigrationRunStatus(BaseModel):
    run_id: str
    job_id: str
    route: Route | None = None
    state: str = Field(json_schema_extra={"enum": list(RUN_STATES)})
    phase: str = Field(json_schema_extra={"enum": list(RUN_PHASES)})
    step: int | None = Field(default=None, ge=1, le=LAST_STEP, description="실행 단계 1~12. 전환 승인 이후에는 null")
    last_step: int = LAST_STEP
    progress: int
    message: str | None = None
    error: RunError | None = None
    database_choices: DatabaseChoices | None = None
    metrics: RunMetrics = {}
    ready_for_cutover: bool = False
    cleanup_required: bool = False
    remaining: list[str] = []


# workflows에도 RunStatus가 있어 API 문서의 이름이 겹치지 않게 모델 이름을 나눴다.
RunStatus = MigrationRunStatus


class RunSummary(BaseModel):
    run_id: str
    route: Route | None = None
    active: bool = Field(description="이 앱에서 지금 실행 중이거나 정리 중")


class RetryRequest(BaseModel):
    """주지 않은 값은 전에 고른 값을 그대로 쓴다."""

    model_config = ConfigDict(extra="forbid")

    source_database: str | None = None
    target_database: str | None = None

    @field_validator("source_database", "target_database")
    @classmethod
    def _single_line(cls, value: str | None) -> str | None:
        if value is not None and not single_line(value):
            raise ValueError("must be a single line of text without surrounding spaces")
        return value


class CloudStatus(BaseModel):
    cloud: str
    registered: bool
    required_keys: list[str]


class CutoverResult(BaseModel):
    run_id: str
    job_id: str
    outcome: CutoverOutcome
    state: str
    phase: str
    message: str
    detail: str | None = None
    hold_ms: float | None = None
    metrics: RunMetrics = {}
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
