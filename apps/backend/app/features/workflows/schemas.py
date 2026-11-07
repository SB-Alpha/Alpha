"""workflows 모듈의 입력·출력 계약.

다른 모듈은 이 파일만 import 합니다. 바꾸려면 받는 쪽 담당자 리뷰가 필요합니다.

- `RunContext`: 각 모듈 `run(inp, ctx)` 이 진행 상황을 보고하는 통로.
- `MigrationRequest`: frontend → workflows 이전 요청. discovery 입력으로 넘어갑니다.
- `RunStatus`: workflows → frontend run 상태 (공통 상태 규격).

임시 사항 (합의되면 교체):
- `MigrationRequest` 필드는 확정서 3절 discovery "받는 것" 기준 제안입니다. 김준석님 `DiscoveryInput` 확정 후 맞춥니다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from app.features.assessment.schemas import ManualSource

# ---------------------------------------------------------------------------
# 상태·단계
# ---------------------------------------------------------------------------


class WorkflowState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class StageName(str, Enum):
    DISCOVERY = "discovery"
    PLANNING = "planning"
    PROVISIONING = "provisioning"
    DB_MIGRATION = "db_migration"
    APP_DEPLOY = "app_deploy"
    CUTOVER = "cutover"


STAGE_ORDER: tuple[StageName, ...] = tuple(StageName)

Gate = Literal["plan", "cutover"]
CleanupState = Literal["running", "done", "failed"]

PreflightStatus = Literal["pass", "fail", "unknown", "info"]
PreflightVerdict = Literal["go", "no_go", "unverified"]
CheckStatus = Literal["pass", "fail", "unknown"]
CutoverDecision = Literal["go", "needs_confirmation", "no_go"]
TargetInfraType = Literal["gcp", "azure", "ncp"]

_SECRET_LIKE_PARAM = re.compile(r"password|passwd|secret|token|api_?key|private_?key|credential", re.IGNORECASE)

# 이 단계를 시작하기 전에 사람의 승인이 필요합니다. ① 계획 승인 ② 전환 승인
GATE_BEFORE: dict[StageName, Gate] = {
    StageName.PROVISIONING: "plan",
    StageName.CUTOVER: "cutover",
}


class RunContext(Protocol):
    """모듈이 실행 중 진행 상황을 보고하는 통로. workflows 가 구현체를 넘겨 줍니다."""

    def report(
        self,
        state: str,
        progress: int,
        metrics: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# 입력 (frontend → workflows)
# ---------------------------------------------------------------------------


class TargetInfraConfig(BaseModel):
    """목적지 클라우드 선택. 해석은 어댑터가 합니다 (docs/TARGET_ADAPTER_SPEC.md)."""

    provider: TargetInfraType = "gcp"
    region: str | None = None
    project_id: str | None = None  # 클라우드별 소속 단위. GCP 는 프로젝트 ID
    extra_params: dict[str, Any] = Field(default_factory=dict)  # 어댑터 전용 값. 비밀은 secret:// 참조만

    @field_validator("extra_params")
    @classmethod
    def _no_raw_secrets(cls, params: dict[str, Any]) -> dict[str, Any]:
        def _check(val: Any, path: str) -> None:
            if isinstance(val, Mapping):
                for k, v in val.items():
                    subpath = f"{path}.{k}"
                    if _SECRET_LIKE_PARAM.search(str(k)):
                        is_reference = isinstance(v, str) and v.startswith("secret://")
                        if not is_reference:
                            raise ValueError(f"{subpath} 에는 원문 대신 secret:// 참조를 넣으세요")
                    _check(v, subpath)
            elif isinstance(val, (list, tuple)):
                for idx, item in enumerate(val):
                    _check(item, f"{path}[{idx}]")

        _check(params, "extra_params")
        return params


class MigrationRequest(BaseModel):
    """이전 요청. 비밀번호·키 원문은 넣지 않고 참조(ARN, Secret 경로)만 받습니다."""

    github_repo_url: str | None = None  # 선택. 이미지는 provisioning 이 원본 ECR 에서 복사합니다 (PR #40)
    service_name: str | None = None  # 원본 서비스 이름. ecs_service 를 비우면 이 값을 씁니다
    # 원본을 읽는 방법. "aws"(기본)는 AWS 계정을 조회하고, "manual" 은 사람이 넣은 manual 값을 씁니다 (assessment #56).
    source_kind: Literal["aws", "manual"] = "aws"
    manual: ManualSource | None = None  # source_kind="manual" 이면 필수. 비밀 원문은 넣지 않습니다
    aws_region: str | None = None  # source_kind="aws" 면 필수
    aws_role_ref: str | None = None  # 원본 조회용 IAM 역할 ARN
    aws_credentials_ref: str | None = None  # 원본 AWS 자격증명 참조 (secret://... 등)
    ecs_service: str | None = None
    rds_instance_id: str | None = None  # 비우면 assessment 가 계정의 RDS 가 하나뿐일 때 고릅니다
    db_credentials_ref: str | None = None  # 예: Secrets Manager ARN. 값 금지
    db_admin_ref: str | None = None  # 원본 DB 관리자 계정 참조
    target_region: str = "asia-northeast3"
    target_infra: TargetInfraConfig | None = None  # 없으면 GCP

    @model_validator(mode="before")
    @classmethod
    def _fill_derived_fields(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        service = values.get("service_name")
        if isinstance(service, str):
            values["service_name"] = service.strip() or None
        if values.get("service_name") and not values.get("ecs_service"):
            values["ecs_service"] = values["service_name"]
        target = values.get("target_infra")
        region = target.get("region") if isinstance(target, Mapping) else getattr(target, "region", None)
        if region and not values.get("target_region"):
            values["target_region"] = region
        return values

    @model_validator(mode="after")
    def _needs_values_for_source(self) -> MigrationRequest:
        if self.source_kind == "aws" and not self.aws_region:
            raise ValueError("source_kind=aws 에는 aws_region 이 필요합니다")
        if self.source_kind == "manual" and self.manual is None:
            raise ValueError("source_kind=manual 에는 manual 이 필요합니다")
        return self

    @model_validator(mode="after")
    def _target_regions_agree(self) -> MigrationRequest:
        region = self.target_infra.region if self.target_infra else None
        if region and region != self.target_region:
            raise ValueError(f"target_region({self.target_region})과 target_infra.region({region})이 다릅니다")
        return self


class ApproveRequest(BaseModel):
    gate: Gate
    comment: str | None = None
    approver: str | None = Field(default=None, max_length=100)
    # 승인 화면에 표시된 점검 결과입니다. 승인자 이름처럼 클라이언트가 보고한 값이며 서버가 검증하지 않습니다.
    preflight_verdict: PreflightVerdict | None = None
    preflight_measured_at: datetime | None = None

    @field_validator("approver", mode="before")
    @classmethod
    def _blank_approver_is_none(cls, value: Any) -> Any:
        # 공백만 있으면 승인자를 모르는 것으로 둡니다.
        return (value.strip() or None) if isinstance(value, str) else value
    # 승인 ② 에서 "미제공" 항목을 알고 진행한다는 운영자 확인. 실패 항목은 이 값으로 넘어가지 못합니다.
    acknowledge_unknown: bool = False


class AbortRequest(BaseModel):
    reason: str | None = None


class ManualStageInput(BaseModel):
    """모듈이 아직 연결되지 않았을 때 사람이 넣는 단계 결과 (fixture). 기록에 manual 로 남습니다."""

    output: dict[str, Any]
    note: str | None = None


# ---------------------------------------------------------------------------
# 출력 (workflows → frontend)
# ---------------------------------------------------------------------------


class StageStatus(BaseModel):
    stage: StageName
    state: WorkflowState = WorkflowState.PENDING
    progress: int = Field(default=0, ge=0, le=100)
    metrics: dict[str, Any] = Field(default_factory=dict)  # 비어 있으면 화면에 "미제공"
    message: str | None = None
    manual: bool = False  # 사람이 넣은 결과로 넘긴 단계
    updated_at: datetime | None = None


class PreflightCheck(BaseModel):
    id: str
    label: str
    status: PreflightStatus
    detail: str | None = None
    value: int | str | None = None


class PreflightReport(BaseModel):
    """전환 승인 ② 전 점검 결과. 참고용(advisory)이며 승인을 막지 않습니다."""

    run_id: str
    gate: Gate = "cutover"
    verdict: PreflightVerdict
    advisory: bool = True
    simulated: bool = False
    checks: list[PreflightCheck]
    measured_at: datetime


class ApprovalRecord(BaseModel):
    """승인 이력. 실패로 승인이 취소돼도 지우지 않습니다."""

    gate: Gate
    approver: str | None = None
    comment: str | None = None
    preflight_verdict: PreflightVerdict | None = None  # 클라이언트가 보고한 값. 승인 ② 만 저장합니다
    preflight_measured_at: datetime | None = None
    readiness_decision: CutoverDecision | None = None  # 서버가 승인 시점에 계산한 승인 조건 판정. 승인 ② 만 저장합니다
    at: datetime


class RunStatus(BaseModel):
    run_id: str
    state: WorkflowState
    current_stage: StageName | None  # 다음에 실행할(또는 실행 중인) 단계. 모두 끝나면 None
    waiting_gate: Gate | None = None
    approvals: list[Gate] = Field(default_factory=list)
    approval_records: list[ApprovalRecord] = Field(default_factory=list)
    request: MigrationRequest
    stages: list[StageStatus]
    outputs: dict[str, Any] = Field(default_factory=dict)  # 단계 이름 → 그 단계 Output (JSON)
    message: str | None = None
    created_at: datetime
    updated_at: datetime
    # 중단 뒤 외부 자원(복제 슬롯 등) 정리 상태. 정리할 것이 없으면 None 입니다.
    cleanup_state: CleanupState | None = None
    cleanup_message: str | None = None

    def stage(self, name: StageName) -> StageStatus:
        return next(s for s in self.stages if s.stage == name)


class ReadinessCheck(BaseModel):
    id: str
    title: str
    status: CheckStatus
    detail: str


class CutoverReadiness(BaseModel):
    decision: CutoverDecision
    checks: list[ReadinessCheck]


class RunDetail(RunStatus):
    """단건 응답. 저장하지 않고 응답할 때 계산한 값을 더합니다."""

    cutover_readiness: CutoverReadiness | None = None  # 승인 ② 대기 중일 때만 채웁니다
    cdc_job_id: str | None = None  # 이 run 의 CDC 에이전트 작업 ID. 직접 제어(/db-migration/jobs/...)에 씁니다
