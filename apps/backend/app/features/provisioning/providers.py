# 목적지 클라우드마다 달라지는 provisioning 설정.
# 새 목적지는 ProviderConfig 를 만들어 PROVIDERS 에 등록한다. service.py 는 이름으로만 고르고 값은 여기서 읽는다.
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import DEFAULT_PROVIDER
from .terraform import ProvisioningError

REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    terraform_dir: Path
    # terraform_dir 를 바꾸는 환경변수. 앞에 있는 것이 먼저 쓰인다.
    terraform_dir_env: tuple[str, ...]
    # 이 목적지의 Terraform 루트가 선언한 변수. target_infra 에서 이 이름만 넘긴다.
    known_variables: frozenset[str]
    default_region: str
    # 목적지 소속 단위(GCP 프로젝트, Azure 구독 등)를 Terraform 에 넘기는 변수 이름,
    # 입력에 없을 때 읽는 환경변수, 오류 메시지에 쓰는 이름
    scope_variable: str
    scope_env: str
    scope_label: str
    # 코드가 항상 덮어쓰는 변수 (예: GCP 의 create_vpn_key)
    forced_variables: Mapping[str, Any] = field(default_factory=dict)


# infra/terraform/gcp 가 선언한 변수들.
GCP = ProviderConfig(
    name="gcp",
    terraform_dir=REPO_ROOT / "infra" / "terraform" / "gcp",
    # PROVISIONING_TF_DIR 은 목적지가 GCP 하나였을 때부터 쓰던 이름이라 GCP 에서만 계속 읽는다.
    terraform_dir_env=("PROVISIONING_TF_DIR_GCP", "PROVISIONING_TF_DIR"),
    known_variables=frozenset(
        {
            "region",
            "service_name",
            "source_image",
            "container_image",
            "container_port",
            "cpu",
            "memory",
            "health_check_path",
            "env_vars",
            "secret_refs",
            "db_version",
            "db_name",
            "db_user",
            "db_tier",
            "db_disk_size_gb",
            "db_logical_decoding",
        }
    ),
    default_region="asia-northeast3",
    scope_variable="project_id",
    scope_env="GCP_PROJECT_ID",
    scope_label="GCP 프로젝트 ID",
    forced_variables={"create_vpn_key": True},
)

PROVIDERS: dict[str, ProviderConfig] = {GCP.name: GCP}


def get_provider(name: str) -> ProviderConfig:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProvisioningError(
            f"목적지 '{name}' 설정이 등록되어 있지 않습니다 (등록된 목적지: {', '.join(sorted(PROVIDERS))})"
        )
    return provider


def terraform_dir(provider: ProviderConfig) -> Path:
    for env_name in provider.terraform_dir_env:
        value = os.getenv(env_name)
        if value:
            return Path(value)
    return provider.terraform_dir
