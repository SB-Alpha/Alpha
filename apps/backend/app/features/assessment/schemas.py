"""assessment 모듈의 입력·출력 계약 (11/4 discovery + planning 통합).

다른 모듈은 이 파일만 import 합니다. 바꾸려면 받는 쪽 담당자 리뷰가 필요합니다.

- 바깥 계약: `AssessmentInput` (workflows → assessment), `AssessmentOutput` (assessment → workflows)
  - `AssessmentOutput.source`: db_migration(안재현) 입력. 칸 이름은 cdc 명세 2절을 따릅니다.
  - `AssessmentOutput.plan`: 승인 ① 화면(박재현) + `plan.target_infra` 는 provisioning(박준서) 입력.
- 내부 계약: `DbAssessment` (db.py, 김준석) / `AppAssessment` (app.py, 이주호) → service.py 가 합칩니다.
- `TargetInfra`: 박준서님 Terraform 변수와 이름·형식을 맞춘 것입니다 (11/3 합의, PR #26).
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator, field_validator

# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------


class AppInfo(BaseModel):
    """원본 컨테이너 앱 정보 (discovery 의 SourceInventory.app 에서 옴)."""

    service_name: str
    image_uri: str | None = None  # 예: <account>.dkr.ecr.<region>.amazonaws.com/target-app:<tag>
    image_digest: str | None = None
    container_port: int | None = None
    cpu_units: int | None = None  # Fargate 단위 (1024 = 1 vCPU)
    memory_mib: int | None = None
    health_check_path: str | None = None
    env_vars: dict[str, str] = Field(default_factory=dict)  # 비밀이 아닌 환경 변수 이름 → 값
    secret_refs: dict[str, str] = Field(default_factory=dict)  # 환경 변수 이름 → AWS 비밀 참조(ARN). 값 금지
    # 목적지에 따라 막힐 수 있는 원본 설정 코드 (예: "privileged", "udp_port", "host_path_volume:data").
    # 옮길 수 있는지는 목적지마다 targets.py 가 판정합니다. None = 태스크 정의를 보지 않음(직접 입력)
    container_settings: list[str] | None = None
    # 옮기되 사람이 확인해야 하는 원본 설정 (예: 시작 명령 재정의, 함께 도는 다른 컨테이너). 값은 넣지 않고 이름만
    container_notes: list[str] = Field(default_factory=list)


class TableInfo(BaseModel):
    schema_name: str = "public"
    name: str
    row_count: int | None = None
    has_primary_key: bool | None = None


class DbInfo(BaseModel):
    """원본 DB 정보 (db.py 가 채움)."""

    engine: str | None = None  # 예: "postgres"
    publicly_accessible: bool | None = None  # RDS 퍼블릭 액세스. 켜져 있으면 CDC 가 VPN 을 못 탐
    engine_version: str | None = None  # 예: "16.4"
    database_name: str | None = None
    username: str | None = None  # 앱 DB 사용자 이름 (비밀번호 아님)
    instance_class: str | None = None  # 예: "db.t3.micro"
    allocated_storage_gb: int | None = None
    tables: list[TableInfo] = Field(default_factory=list)
    logical_replication: bool | None = None  # rds.logical_replication = 1 여부
    wal_level: str | None = None
    # 클라우드 중립 DB 크기. 직접 입력처럼 인스턴스 클래스가 없는 원본에서 채웁니다. 없으면 클래스 표로 계산
    memory_gib: float | None = None
    vcpu: int | None = None
    # 복제 슬롯 여유 (PG 카탈로그 조회). CDC 가 슬롯을 하나 만들어야 합니다. 모르면 None
    max_replication_slots: int | None = None
    replication_slots_in_use: int | None = None
    ha_enabled: bool | None = None  # 고가용성 여부 (Multi-AZ / REGIONAL / zone redundant)
    backup_retention_days: int | None = None  # 자동 백업 보관 일수


class InventoryForPlanning(BaseModel):
    """판정·매핑(plan.py)이 읽는 원본 사실. db.py·app.py 결과를 service.py 가 합쳐 만듭니다."""

    app: AppInfo
    db: DbInfo
    gaps: list[str] = Field(default_factory=list)  # 조회하지 못한 항목
    # 이전 실행(db_migration)에 넘길 원본 정보. 주면 "이 정보로 실제 이전을 시작할 수 있는가"도 판정합니다.
    # 직접 사실을 넣는 /assessment/plan 처럼 모르면 None 이고, 그때는 이 검사를 하지 않습니다.
    source: "SourceInfo | None" = None


# 목적지 클라우드. workflows 의 TargetInfraType 과 같은 값. 호환 규칙이 없는 목적지(ncp)는 호환 검사가 unknown
TargetCloud = Literal["gcp", "aws", "azure", "ncp", "onprem"]


class TargetSelection(BaseModel):
    """사용자가 고른 목적지. 호환 검사·안내 문구가 이 클라우드 기준으로 바뀝니다 (targets.py)."""

    cloud: TargetCloud = "gcp"
    region: str  # 예: "asia-northeast3"


class PlanningInput(BaseModel):
    inventory: InventoryForPlanning
    target: TargetSelection


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

CheckResult = Literal["pass", "fail", "unknown"]


class PlanCheck(BaseModel):
    """코드로 검사한 필수 조건. AI 판단이 아닙니다."""

    id: Literal[
        "pg_version_compatible",
        "logical_replication_enabled",
        "primary_keys_present",
        "table_names_supported",
        "replication_slot_available",
        "target_platform_compatible",
        "image_available",
        "source_executable",
    ]
    title: str
    result: CheckResult
    detail: str


class Risk(BaseModel):
    level: Literal["high", "medium", "low"]
    title: str
    detail: str
    source: Literal["rule", "ai"] = "rule"


class PlanStep(BaseModel):
    order: int
    module: Literal["provisioning", "db_migration", "traffic", "workflows"]
    action: str
    needs_approval: bool = False


class TargetInfra(BaseModel):
    """provisioning(Terraform) 입력. 필드 이름 = Terraform 변수 이름 (박준서님과 합의).

    중첩 없는 평평한 구조여야 그대로 tfvars 로 쓸 수 있습니다.
    project_id 는 provisioning 이 따로 넣습니다.
    source_service_name · db_memory_gib · db_vcpu 는 GCP Terraform 변수가 아니라 다른 목적지(Azure 등)가
    자기 규칙으로 바꿔 쓰는 클라우드 중립 값입니다. GCP provisioning 은 선언된 변수만 골라 쓰므로 영향이 없습니다.
    """

    region: str
    service_name: str  # GCP 자원 이름 규칙을 적용한 이름
    source_service_name: str | None = None  # 규칙 적용 전 원래 이름. 목적지마다 자기 이름 규칙을 적용 (클라우드 중립)
    source_image: str | None  # 원본 ECR 주소. Artifact Registry 복사는 provisioning 이 함
    container_port: int
    cpu: int  # vCPU
    memory: int  # MiB
    health_check_path: str
    env_vars: dict[str, str]  # 비밀 아닌 값. PG*·APP_ENV·PORT 는 provisioning 이 채움
    secret_refs: dict[str, str]  # 환경 변수 이름 → GCP Secret Manager 비밀 이름
    db_version: str  # 예: "16"
    db_name: str | None
    db_user: str | None
    db_tier: str | None  # Cloud SQL 등급(GCP 전용). None 이면 provisioning 기본값
    # 클라우드 중립 DB 크기: 원본 인스턴스의 메모리·vCPU. 목적지마다 자기 등급으로 바꿔 씁니다. 모르면 None
    db_memory_gib: float | None = None
    db_vcpu: int | None = None
    db_disk_size_gb: int | None  # None 이면 provisioning 기본값
    db_logical_decoding: bool = True  # CDC 대상이라 항상 켬

    def to_tfvars(self) -> dict:
        """Terraform tfvars(JSON) 로 그대로 쓰는 dict. 값이 없는 키는 빼서 provisioning 기본값을 쓰게 함."""
        return self.model_dump(exclude_none=True)


class Suggestion(BaseModel):
    """AI 가 덧붙인 제안. 판정(checks · approvable)에는 쓰이지 않고 승인자가 참고만 합니다."""

    kind: Literal["service_mapping", "missing_dependency", "needs_confirmation"]
    subject: str  # 근거가 된 환경 변수 이름. 입력에 실제로 있는 이름만 허용
    observation: str  # 무엇이 보였는지
    suggestion: str  # 목적지에서 어떻게 하면 좋을지
    confidence: Literal["high", "medium", "low"]
    source: Literal["ai"] = "ai"


class MigrationPlan(BaseModel):
    target_infra: TargetInfra
    checks: list[PlanCheck]
    risks: list[Risk]
    summary: list[str]  # 승인 화면용 3줄
    summary_source: Literal["rule", "ai"] = "rule"
    steps: list[PlanStep]
    approvable: bool  # 모든 필수 조건이 pass 일 때만 True
    blocking: list[str] = Field(default_factory=list)  # approvable=False 인 이유
    suggestions: list[Suggestion] = Field(default_factory=list)  # AI 제안(참고용). 꺼져 있으면 항상 빈 목록
    generated_at: datetime


# ---------------------------------------------------------------------------
# assessment 바깥 계약
# ---------------------------------------------------------------------------


class ManualApp(BaseModel):
    """직접 입력한 원본 앱. 모르는 값은 비워 두면 기본값·gaps 로 처리합니다."""

    service_name: str
    image_uri: str | None = None
    container_port: int | None = None
    vcpu: float | None = None
    memory_mib: int | None = None
    health_check_path: str | None = None
    env_vars: dict[str, str] = Field(default_factory=dict)  # 비밀 아닌 값만. 비밀처럼 보이면 버리고 gaps 에 남김
    db_user: str | None = None  # 앱이 쓰는 DB 사용자 이름 (비밀번호 아님)


class ManualDb(BaseModel):
    """직접 입력한 원본 PostgreSQL. 모르는 값은 비워 두면 '확인 안 됨' 으로 판정합니다."""

    engine_version: str | None = None  # 예: "16.4"
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    memory_gib: float | None = None
    vcpu: int | None = None
    storage_gb: int | None = None
    publicly_accessible: bool | None = None
    logical_replication: bool | None = None
    tables: list[TableInfo] = Field(default_factory=list)


class ManualSource(BaseModel):
    """source_kind="manual" 일 때의 원본 정보. AWS 가 아닌 원본(GCP·Azure·온프레미스 등)용."""

    cloud: str  # 원본이 있는 곳. 예: "gcp", "azure", "onprem"
    region: str | None = None
    app: ManualApp
    db: ManualDb = Field(default_factory=ManualDb)


class CloudSource(BaseModel):
    """source_kind="gcp" / "azure" 일 때 원본 위치. 비밀은 이름표만 받습니다.

    - GCP: project_id · region · app_name(Cloud Run 서비스) · db_instance(Cloud SQL 인스턴스).
      credentials_ref 는 서비스 계정 키 JSON 이름표, 비우면 서버의 ADC 로그인을 씁니다.
    - Azure: subscription_id · resource_group · app_name(Container App) · db_instance(Flexible Server).
      credentials_ref 는 {tenant_id, client_id, client_secret} 이름표 (필수).
    """

    region: str  # GCP 리전 / Azure 위치. 예: "asia-northeast3", "koreacentral"
    app_name: str
    db_instance: str
    credentials_ref: str | None = None
    project_id: str | None = None  # GCP
    subscription_id: str | None = None  # Azure
    resource_group: str | None = None  # Azure

    @field_validator("region", "app_name", "db_instance", "project_id", "subscription_id", "resource_group")
    @classmethod
    def _url_safe_name(cls, value: str | None) -> str | None:
        """클라우드 API 주소 경로에 그대로 들어가는 값이라 /·?·#·% 와 공백, '.'·'..' 은 받지 않습니다."""
        if value is None:
            return value
        if not value or value in (".", "..") or re.search(r"[/?#\%\s]", value):
            raise ValueError("이름에는 / ? # % 공백을 쓸 수 없고 비어 있거나 '.'·'..' 일 수 없습니다")
        return value

    @field_validator("credentials_ref")
    @classmethod
    def _ref_shape(cls, value: str | None) -> str | None:
        """비우면 GCP 는 서버 로그인(ADC)을 씁니다. 값이 있으면 secret://… 이름표여야 하고, 아니면 거부합니다."""
        if value is None or value == "":
            return None
        if not value.startswith("secret://"):
            raise ValueError("credentials_ref 는 secret://… 이름표여야 합니다 (비우면 GCP 는 서버 로그인을 씁니다)")
        return value


class AssessmentInput(BaseModel):
    """workflows → assessment. 비밀은 원문이 아니라 이름표(secret://...)만 받습니다.

    source_kind 로 원본을 읽는 방법을 고릅니다. 어느 쪽이든 결과(AssessmentOutput)는 같은 형식입니다.
    - "aws"(기본): AWS 계정을 조회합니다. aws_credentials_ref · aws_region 이 필요합니다.
    - "manual": 사람이 입력한 manual 값으로 만듭니다. 원본에서 확인한 값이 아니라 gaps 에 남깁니다.
    - "gcp" / "azure": cloud_source 위치의 앱·DB 를 클라우드 API 로 조회합니다 (cloud_source.py).
      db_instance_id·gcp_project·azure_subscription_id 는 중계 VM 으로 읽는 경로(db.py: gcp_sql · azure_pg)의 입력입니다. 아직 service 가 부르지 않습니다.
    """

    run_id: str | None = None
    source_kind: Literal["aws", "manual", "gcp", "azure"] = "aws"
    github_repo_url: str | None = None
    aws_credentials_ref: str | None = None  # 원본 AWS 키 이름표. source_kind="aws" 면 필수
    db_admin_ref: str | None = None  # 원본 DB 관리자 계정 이름표 (db.py 가 PG 카탈로그 조회에 씀)
    aws_region: str | None = None  # 원본 AWS 리전. 예: "ap-northeast-2". source_kind="aws" 면 필수
    target_region: str  # 목적지 리전. 예: "asia-northeast3"
    target_cloud: TargetCloud = "gcp"  # 목적지 클라우드. workflows 의 target_infra.provider 와 같은 값
    manual: ManualSource | None = None  # source_kind="manual" 이면 필수
    cloud_source: CloudSource | None = None  # source_kind="gcp"·"azure" 면 필수
    # 원본 계정에 후보가 여러 개일 때만 지정. 비우면 하나뿐일 때 자동으로 고릅니다.
    ecs_cluster: str | None = None
    ecs_service: str | None = None
    rds_instance_id: str | None = None
    db_instance_id: str | None = None  # 원본 DB 식별. gcp=인스턴스 이름, azure="<리소스그룹>/<서버>", 비우면 rds_instance_id
    gcp_project: str | None = None  # 비우면 환경변수 ALPHA_GCP_PROJECT
    azure_subscription_id: str | None = None  # 비우면 환경변수 ALPHA_AZURE_SUBSCRIPTION_ID

    @model_validator(mode="after")
    def _needs_values_for_kind(self) -> AssessmentInput:
        if self.source_kind == "aws":
            missing = [n for n in ("aws_credentials_ref", "aws_region") if not getattr(self, n)]
            if missing:
                raise ValueError(f"source_kind=aws 에는 {', '.join(missing)} 가 필요합니다")
        elif self.source_kind == "manual":
            if self.manual is None:
                raise ValueError("source_kind=manual 에는 manual 이 필요합니다")
        else:
            src = self.cloud_source
            if src is None:
                raise ValueError(f"source_kind={self.source_kind} 에는 cloud_source 가 필요합니다")
            need = ("project_id",) if self.source_kind == "gcp" else ("subscription_id", "resource_group",
                                                                     "credentials_ref")
            missing = [n for n in need if not getattr(src, n)]
            if missing:
                raise ValueError(f"source_kind={self.source_kind} 의 cloud_source 에 {', '.join(missing)} 가 필요합니다")
        return self


class SourceDb(BaseModel):
    instance_id: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    version: str | None = None
    publicly_accessible: bool | None = None
    logical_replication: bool | None = None


class SourceNetwork(BaseModel):
    vpc_id: str | None = None
    cidr: str | None = None
    route_table_ids: list[str] = Field(default_factory=list)
    db_security_group_id: str | None = None
    allowed_cidrs: list[str] = Field(default_factory=list)  # DB 접근 허용 대역 요약 (GCP authorized networks / Azure 방화벽 / AWS SG 인바운드)


class SourceApp(BaseModel):
    db_user: str | None = None  # 앱 설정의 PGUSER


class SourceCredentials(BaseModel):
    cloud: str | None = None  # AWS 키 이름표
    db_admin: str | None = None  # DB 관리자 계정 이름표


class SourceInfo(BaseModel):
    """db_migration 입력. 칸 이름 = cdc 명세 2절 (안재현). 모르는 값은 None 으로 둡니다."""

    cloud: str = "aws"  # 원본이 있는 클라우드. db_migration 의 SourceInput.cloud 와 같은 값 (aws · gcp · azure · onprem)
    account_id: str | None = None
    region: str | None = None
    db: SourceDb = Field(default_factory=SourceDb)
    network: SourceNetwork = Field(default_factory=SourceNetwork)
    app: SourceApp = Field(default_factory=SourceApp)
    tables: list[str] = Field(default_factory=list)  # "schema.table"
    credentials_ref: SourceCredentials = Field(default_factory=SourceCredentials)


class AssessmentOutput(BaseModel):
    source: SourceInfo
    plan: MigrationPlan
    gaps: list[str] = Field(default_factory=list)  # 조회 실패 항목. 값은 지어내지 않음


# ---------------------------------------------------------------------------
# assessment 내부 계약 (db.py · app.py → service.py)
# ---------------------------------------------------------------------------


class DbAssessment(BaseModel):
    """db.py(김준석) 결과: DB·네트워크 조회 사실."""

    facts: DbInfo = Field(default_factory=DbInfo)
    source_db: SourceDb = Field(default_factory=SourceDb)
    network: SourceNetwork = Field(default_factory=SourceNetwork)
    region: str | None = None
    account_id: str | None = None
    gaps: list[str] = Field(default_factory=list)


class AppAssessment(BaseModel):
    """app.py(이주호) 결과: ECS·ECR 조회 사실."""

    facts: AppInfo
    account_id: str | None = None
    db_user: str | None = None  # 앱 설정 PGUSER
    gaps: list[str] = Field(default_factory=list)
