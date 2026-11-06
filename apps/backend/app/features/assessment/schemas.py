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
from typing import Literal

from pydantic import BaseModel, Field

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


class InventoryForPlanning(BaseModel):
    """판정·매핑(plan.py)이 읽는 원본 사실. db.py·app.py 결과를 service.py 가 합쳐 만듭니다."""

    app: AppInfo
    db: DbInfo
    gaps: list[str] = Field(default_factory=list)  # 조회하지 못한 항목


class TargetSelection(BaseModel):
    """사용자가 고른 목적지."""

    cloud: Literal["gcp"] = "gcp"
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
        "image_available",
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


class MigrationPlan(BaseModel):
    target_infra: TargetInfra
    checks: list[PlanCheck]
    risks: list[Risk]
    summary: list[str]  # 승인 화면용 3줄
    summary_source: Literal["rule", "ai"] = "rule"
    steps: list[PlanStep]
    approvable: bool  # 모든 필수 조건이 pass 일 때만 True
    blocking: list[str] = Field(default_factory=list)  # approvable=False 인 이유
    generated_at: datetime


# ---------------------------------------------------------------------------
# assessment 바깥 계약
# ---------------------------------------------------------------------------


class AssessmentInput(BaseModel):
    """workflows → assessment. 비밀은 원문이 아니라 이름표(secret://...)만 받습니다."""

    run_id: str | None = None
    github_repo_url: str | None = None
    aws_credentials_ref: str  # 원본 AWS 키 이름표
    db_admin_ref: str | None = None  # 원본 DB 관리자 계정 이름표 (db.py 가 PG 카탈로그 조회에 씀)
    aws_region: str  # 원본 AWS 리전. 예: "ap-northeast-2"
    target_region: str  # 목적지 GCP 리전. 예: "asia-northeast3"
    # 원본 계정에 후보가 여러 개일 때만 지정. 비우면 하나뿐일 때 자동으로 고릅니다.
    ecs_cluster: str | None = None
    ecs_service: str | None = None
    rds_instance_id: str | None = None


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


class SourceApp(BaseModel):
    db_user: str | None = None  # 앱 설정의 PGUSER


class SourceCredentials(BaseModel):
    cloud: str | None = None  # AWS 키 이름표
    db_admin: str | None = None  # DB 관리자 계정 이름표


class SourceInfo(BaseModel):
    """db_migration 입력. 칸 이름 = cdc 명세 2절 (안재현). 모르는 값은 None 으로 둡니다."""

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
    gaps: list[str] = Field(default_factory=list)


class AppAssessment(BaseModel):
    """app.py(이주호) 결과: ECS·ECR 조회 사실."""

    facts: AppInfo
    account_id: str | None = None
    db_user: str | None = None  # 앱 설정 PGUSER
    gaps: list[str] = Field(default_factory=list)
