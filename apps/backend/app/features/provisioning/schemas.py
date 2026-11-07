# provisioning 단계의 입력과 출력 모양.
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

# 입력으로 받을 수 있는 목적지 이름. 등록된 이름(providers.PROVIDERS)과 같아야 하고, 같은지는 tests/provisioning/test_providers.py 가 확인한다.
# 이 파일이 구현(providers, terraform)을 import 하지 않게 이름만 여기에 둔다.
ProviderName = Literal["gcp"]
DEFAULT_PROVIDER: ProviderName = "gcp"


class ProvisioningInput(BaseModel):
    """목적지 인프라를 만드는 단계(provisioning)의 입력."""

    run_id: str
    target_infra: dict[str, Any]
    # 목적지 클라우드. 등록된 이름만 받는다(오타는 입력 검증에서 바로 걸린다). 비우면 gcp.
    provider: ProviderName = DEFAULT_PROVIDER
    # 목적지 소속 단위(GCP 프로젝트 ID, Azure 구독 ID 등). 비우면 provider 가 정한 환경변수를 읽는다.
    project_id: str | None = None
    # 계획에 기존 리소스 삭제가 들어 있어도 진행할지. 기본은 막음.
    allow_destroy: bool = False
    # True 면 계획만 세우고 적용하지 않는다. 지금 인프라와 얼마나 다른지 미리 볼 때 쓰는 용도.
    dry_run: bool = False


class ProvisioningOutput(BaseModel):
    """provisioning 단계의 결과. workflows 가 이것으로 db_migration 의 입력(target)을 조립한다."""

    target: dict[str, Any]
    # CDC 가 목적지에 닿지 못할 수 있는 설정 문제 (target.find_problems). 비어 있으면 문제 없음.
    problems: list[str] = Field(default_factory=list)
    changes: dict[str, int]
    # 실제로 terraform apply 를 했는지. 바뀔 것이 없거나 dry_run 이면 False.
    applied: bool


class AppDeployInput(BaseModel):
    """목적지 앱(Cloud Run)을 배포하는 단계(app_deploy)의 입력."""

    run_id: str
    # provisioning 때와 같은 값을 다시 넘김.
    target_infra: dict[str, Any]
    provider: ProviderName = DEFAULT_PROVIDER  # provisioning 때와 같은 값을 다시 넘김.
    project_id: str | None = None
    allow_destroy: bool = False
    dry_run: bool = False

    source_credentials_ref: str | None = None

    health_timeout_s: float = 120.0
    # True 면 앱이 준비된 뒤 Worker 의 GCP 쪽 원본 주소를 새 Cloud Run 주소로 바꾼다. 환경변수 WORKER_URL 이 필요하다.
    update_worker_target: bool = True


class AppDeployOutput(BaseModel):
    """app_deploy 단계의 결과."""

    app_url: str | None  # 배포된 Cloud Run 주소.
    healthy: bool  # /health/ready 가 200 이었는지
    target: dict[str, Any]
    changes: dict[str, int]
    applied: bool
    worker_target_updated: bool = False  # Worker 의 원본 주소를 app_url 로 바꿨는지
