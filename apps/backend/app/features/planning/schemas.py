"""planning 모듈의 입력·출력 계약.

다른 모듈은 이 파일만 import 합니다. 바꾸려면 받는 쪽 담당자 리뷰가 필요합니다.

임시 사항 (합의되면 교체):
- `InventoryForPlanning`: discovery 의 `SourceInventory` 가 나오기 전까지 planning 이
  실제로 읽는 필드만 적어 둔 것입니다. SourceInventory 확정 후 그 모델로 바꿉니다.
- `TargetInfra` 필드 이름: 박준서님 Terraform 변수 목록이 나오면 그 이름과 1:1 로 맞춥니다.
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
    env_names: list[str] = Field(default_factory=list)  # 이름만, 값 금지
    secret_refs: list[str] = Field(default_factory=list)  # Secrets Manager ARN 등 참조만


class TableInfo(BaseModel):
    schema_name: str = "public"
    name: str
    row_count: int | None = None
    has_primary_key: bool | None = None


class DbInfo(BaseModel):
    """원본 DB 정보 (discovery 의 SourceInventory.db 에서 옴)."""

    engine: str | None = None  # 예: "postgres"
    engine_version: str | None = None  # 예: "16.4"
    database_name: str | None = None
    tables: list[TableInfo] = Field(default_factory=list)
    logical_replication: bool | None = None  # rds.logical_replication = 1 여부
    wal_level: str | None = None


class InventoryForPlanning(BaseModel):
    """planning 이 SourceInventory 에서 읽는 부분. (임시 — SourceInventory 확정 후 교체)"""

    app: AppInfo
    db: DbInfo
    gaps: list[str] = Field(default_factory=list)  # discovery 가 수집하지 못한 항목


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
    """provisioning(Terraform) 입력. 이름은 Terraform 변수와 1:1 로 맞출 예정 (박준서 확인 필요)."""

    region: str
    service_name: str
    source_image: str | None  # 원본 ECR 이미지. None 이면 이미지 확보 불가
    container_port: int
    cpu: str  # Cloud Run 형식 예: "1"
    memory: str  # Cloud Run 형식 예: "512Mi"
    health_check_path: str
    env_names: list[str]
    secret_refs: list[str]
    db_version: str  # Cloud SQL 형식 예: "POSTGRES_16"
    db_name: str | None
    db_logical_decoding: bool = True  # CDC 대상이라 항상 켬
    db_private_ip: bool = True

    def to_tfvars(self) -> dict:
        """Terraform tfvars(JSON) 로 그대로 쓰는 dict."""
        return self.model_dump()


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
