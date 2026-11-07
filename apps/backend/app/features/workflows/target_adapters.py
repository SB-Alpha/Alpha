"""목적지 클라우드별 구축·배포 연결.

새 클라우드는 어댑터를 만들어 `TARGET_ADAPTERS` 에 등록하면 runner 와 schemas 를 고치지 않고 붙습니다.
입출력 계약은 docs/TARGET_ADAPTER_SPEC.md 에 있습니다. 등록되지 않은 클라우드는 다른 클라우드를
대신 구축하지 않도록 요청 단계에서 거절합니다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

from .schemas import MigrationRequest, RunContext, TargetInfraConfig, TargetInfraType

DEFAULT_PROVIDER: TargetInfraType = "gcp"


class UnsupportedTarget(ValueError):
    """요청한 목적지 클라우드의 어댑터가 등록되지 않았을 때."""


class TargetAdapter(Protocol):
    provider: TargetInfraType

    def provision(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any: ...

    def deploy_app(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any: ...


def _project_id(config: TargetInfraConfig | None) -> str | None:
    return config.project_id if config else None


class GcpTargetAdapter:
    """Cloud Run + Cloud SQL. 프로젝트 ID 를 요청에 주지 않으면 provisioning 이 GCP_PROJECT_ID 를 씁니다."""

    provider: TargetInfraType = "gcp"

    def provision(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import ProvisioningInput

        inp = ProvisioningInput(run_id=run_id, target_infra=target_infra, project_id=_project_id(config))
        return provisioning_service.run(inp, ctx)

    def deploy_app(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import AppDeployInput

        inp = AppDeployInput(
            run_id=run_id,
            target_infra=target_infra,
            project_id=_project_id(config),
            update_worker_target=bool(os.environ.get("WORKER_URL")),
        )
        return provisioning_service.deploy_app(inp, ctx)


TARGET_ADAPTERS: Mapping[TargetInfraType, TargetAdapter] = {"gcp": GcpTargetAdapter()}


def provider_of(request: MigrationRequest) -> TargetInfraType:
    return request.target_infra.provider if request.target_infra else DEFAULT_PROVIDER


def adapter_for(request: MigrationRequest) -> TargetAdapter:
    provider = provider_of(request)
    adapter = TARGET_ADAPTERS.get(provider)
    if adapter is None:
        raise UnsupportedTarget(f"목적지 '{provider}' 어댑터가 아직 등록되지 않았습니다")
    return adapter
