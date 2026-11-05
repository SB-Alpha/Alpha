"""workflows 모듈의 입력·출력 계약.

다른 모듈은 이 파일만 import 합니다. 바꾸려면 받는 쪽 담당자 리뷰가 필요합니다.

- `RunContext`: 각 모듈 `run(inp, ctx)` 이 진행 상황을 보고하는 통로.
- `MigrationRequest`: frontend → workflows 이전 요청. discovery 입력으로 넘어갑니다.
- `RunStatus`: workflows → frontend run 상태 (공통 상태 규격).

임시 사항 (합의되면 교체):
- `MigrationRequest` 필드는 확정서 3절 discovery "받는 것" 기준 제안입니다. 김준석님 `DiscoveryInput` 확정 후 맞춥니다.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

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


class MigrationRequest(BaseModel):
    """이전 요청. 비밀번호·키 원문은 넣지 않고 참조(ARN, Secret 경로)만 받습니다."""

    github_repo_url: str
    aws_region: str
    aws_role_ref: str | None = None  # 원본 조회용 IAM 역할 ARN
    aws_credentials_ref: str | None = None  # 원본 AWS 자격증명 참조 (secret://... 등)
    ecs_service: str | None = None
    rds_instance_id: str | None = None
    db_credentials_ref: str | None = None  # 예: Secrets Manager ARN. 값 금지
    db_admin_ref: str | None = None  # 원본 DB 관리자 계정 참조
    target_region: str = "asia-northeast3"


class ApproveRequest(BaseModel):
    gate: Gate
    comment: str | None = None


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


class RunStatus(BaseModel):
    run_id: str
    state: WorkflowState
    current_stage: StageName | None  # 다음에 실행할(또는 실행 중인) 단계. 모두 끝나면 None
    waiting_gate: Gate | None = None
    approvals: list[Gate] = Field(default_factory=list)
    request: MigrationRequest
    stages: list[StageStatus]
    outputs: dict[str, Any] = Field(default_factory=dict)  # 단계 이름 → 그 단계 Output (JSON)
    message: str | None = None
    created_at: datetime
    updated_at: datetime

    def stage(self, name: StageName) -> StageStatus:
        return next(s for s in self.stages if s.stage == name)
