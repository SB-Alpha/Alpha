"""목적지 클라우드별 구축·배포 연결.

새 클라우드는 어댑터를 만들어 `TARGET_ADAPTERS` 에 등록하면 runner 와 schemas 를 고치지 않고 붙습니다.
입출력 계약은 docs/TARGET_ADAPTER_SPEC.md 에 있습니다. 등록되지 않은 클라우드는 다른 클라우드를
대신 구축하지 않도록 요청 단계에서 거절합니다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Protocol, get_args

from .schemas import MigrationRequest, RunContext, TargetInfo, TargetInfraConfig, TargetInfraType

DEFAULT_PROVIDER: TargetInfraType = "gcp"


class UnsupportedTarget(ValueError):
    """요청한 목적지 클라우드의 어댑터가 등록되지 않았을 때."""


class TargetAdapter(Protocol):
    provider: TargetInfraType

    def provision(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any: ...

    # source_credentials_ref: 원본 AWS 키 이름표. 원본 이미지를 목적지 저장소로 복사할 때만 쓰고, 없을 수 있습니다.
    def deploy_app(
        self,
        run_id: str,
        target_infra: dict[str, Any],
        config: TargetInfraConfig | None,
        ctx: RunContext,
        source_credentials_ref: str | None = None,
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
        self,
        run_id: str,
        target_infra: dict[str, Any],
        config: TargetInfraConfig | None,
        ctx: RunContext,
        source_credentials_ref: str | None = None,
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import AppDeployInput

        inp = AppDeployInput(
            run_id=run_id,
            target_infra=target_infra,
            project_id=_project_id(config),
            source_credentials_ref=source_credentials_ref,
            update_worker_target=bool(os.environ.get("WORKER_URL")),
        )
        return provisioning_service.deploy_app(inp, ctx)



def _azure_infra(target_infra: dict[str, Any], config: TargetInfraConfig | None) -> dict[str, Any]:
    """조사 결과에 요청의 Azure 값을 얹는다: 리전은 요청 값이 우선, 리소스 그룹은 extra_params 에서 받는다."""
    infra = dict(target_infra)
    if config is None:
        return infra
    if config.region:
        infra["region"] = config.region
    group = config.extra_params.get("resource_group") or config.extra_params.get("resource_group_name")
    if group:
        infra["resource_group_name"] = group
    return infra


class AzureTargetAdapter:
    """Container Apps + PostgreSQL Flexible Server (infra/azure).

    구독 ID 는 요청의 project_id, 없으면 환경변수 AZURE_SUBSCRIPTION_ID 를 씁니다.
    앱 크기(cpu/memory)는 provisioning 이 Azure 규칙에 맞춰 바꾸고, DB 등급(db_tier)은 넘기지 않습니다.
    """
    provider: TargetInfraType = "azure"

    def provision(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import ProvisioningInput

        inp = ProvisioningInput(
            run_id=run_id,
            target_infra=_azure_infra(target_infra, config),
            provider=self.provider,
            project_id=_project_id(config),
        )
        return provisioning_service.run(inp, ctx)

    def deploy_app(
        self,
        run_id: str,
        target_infra: dict[str, Any],
        config: TargetInfraConfig | None,
        ctx: RunContext,
        source_credentials_ref: str | None = None,
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import AppDeployInput

        inp = AppDeployInput(
            run_id=run_id,
            target_infra=_azure_infra(target_infra, config),
            provider=self.provider,
            project_id=_project_id(config),
            source_credentials_ref=source_credentials_ref,
            update_worker_target=bool(os.environ.get("WORKER_URL")),
        )
        return provisioning_service.deploy_app(inp, ctx)


# ap-northeast-2, us-east-1, us-gov-west-1 처럼 AWS 리전 이름 모양
_AWS_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


def _aws_infra(target_infra: dict[str, Any], config: TargetInfraConfig | None) -> tuple[dict[str, Any], str | None]:
    """조사 결과에 요청의 AWS 값을 얹는다: 리전은 요청 값이 우선이다.

    요청에 리전이 없으면 MigrationRequest.target_region 의 기본값(asia-northeast3, GCP 리전)이 그대로 오는데, 그 값으로는
    AWS Terraform 이 실패한다. AWS 리전 모양이 아니면 리전을 빼서 AWS 설정 카드의 기본 리전을 쓰게 하고, 뺀 값을 함께 돌려준다.
    """
    infra = dict(target_infra)
    if config is not None and config.region:
        infra["region"] = config.region
    region = infra.get("region")
    if region and not _AWS_REGION.match(str(region)):
        del infra["region"]
        return infra, str(region)
    return infra, None


def _say_region_dropped(ctx: RunContext, dropped: str | None) -> None:
    if dropped:
        ctx.report("running", 1, message=f"'{dropped}' 는 AWS 리전이 아니라 기본 리전(ap-northeast-2)을 씁니다")


class AwsTargetAdapter:
    """ECS Fargate + ALB + RDS PostgreSQL (infra/terraform/aws).

    계정 ID 는 요청의 project_id, 없으면 환경변수 AWS_ACCOUNT_ID 를 씁니다. 둘 다 없으면 시작하지 않습니다.
    어느 계정에 만들지는 백엔드를 실행하는 환경의 AWS 로그인(환경변수, AWS_PROFILE)이 정하고,
    계정 ID 는 그 로그인이 맞는 계정인지 확인하는 데 쓰입니다 (다르면 Terraform 이 거부합니다).
    앱 크기(cpu/memory)는 provisioning 이 Fargate 규칙에 맞춰 바꾸고, DB 등급은 넘기지 않습니다.
    """

    provider: TargetInfraType = "aws"

    def provision(
        self, run_id: str, target_infra: dict[str, Any], config: TargetInfraConfig | None, ctx: RunContext
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import ProvisioningInput

        infra, dropped = _aws_infra(target_infra, config)
        _say_region_dropped(ctx, dropped)
        inp = ProvisioningInput(
            run_id=run_id,
            target_infra=infra,
            provider=self.provider,
            project_id=_project_id(config),
        )
        return provisioning_service.run(inp, ctx)

    def deploy_app(
        self,
        run_id: str,
        target_infra: dict[str, Any],
        config: TargetInfraConfig | None,
        ctx: RunContext,
        source_credentials_ref: str | None = None,
    ) -> Any:
        from app.features.provisioning import service as provisioning_service
        from app.features.provisioning.schemas import AppDeployInput

        infra, dropped = _aws_infra(target_infra, config)
        _say_region_dropped(ctx, dropped)
        inp = AppDeployInput(
            run_id=run_id,
            target_infra=infra,
            provider=self.provider,
            project_id=_project_id(config),
            source_credentials_ref=source_credentials_ref,
            update_worker_target=bool(os.environ.get("WORKER_URL")),
        )
        return provisioning_service.deploy_app(inp, ctx)


class OnpremTargetAdapter:
    """OrbStack + Cloudflare. 기존 ProvisioningOutput/AppDeployOutput 계약 유지."""
    provider: TargetInfraType = "onprem"

    @staticmethod
    def _ref(config):
        ref = config.extra_params.get("onprem_config_ref") if config else None
        ref = ref or os.getenv("ONPREM_CONFIG_REF")
        if not isinstance(ref, str) or not ref.startswith("secret://"):
            raise UnsupportedTarget("운영 설정 ONPREM_CONFIG_REF에 온프렘 secret:// 설정 참조가 필요합니다")
        return ref

    def provision(self, run_id, target_infra, config, ctx):
        from app.features.provisioning.onprem import service
        return service.provision(run_id=run_id, config_ref=self._ref(config), ctx=ctx)

    def deploy_app(self, run_id, target_infra, config, ctx, source_credentials_ref=None):
        from app.features.provisioning.onprem import service
        return service.deploy_app(run_id=run_id, config_ref=self._ref(config),
                                  target_infra=target_infra, ctx=ctx,
                                  source_credentials_ref=source_credentials_ref)


TARGET_ADAPTERS: Mapping[TargetInfraType, TargetAdapter] = {
    "gcp": GcpTargetAdapter(),
    "azure": AzureTargetAdapter(),
    "aws": AwsTargetAdapter(),
    "onprem": OnpremTargetAdapter(),
}


def describe_targets() -> list[TargetInfo]:
    """화면의 목적지 선택지. 손으로 옮겨 적은 표 대신 등록된 어댑터에서 바로 만듭니다."""
    return [TargetInfo(provider=p, available=p in TARGET_ADAPTERS) for p in get_args(TargetInfraType)]


def provider_of(request: MigrationRequest) -> TargetInfraType:
    return request.target_infra.provider if request.target_infra else DEFAULT_PROVIDER


def adapter_for(request: MigrationRequest) -> TargetAdapter:
    provider = provider_of(request)
    adapter = TARGET_ADAPTERS.get(provider)
    if adapter is None:
        raise UnsupportedTarget(f"목적지 '{provider}' 어댑터가 아직 등록되지 않았습니다")
    return adapter
