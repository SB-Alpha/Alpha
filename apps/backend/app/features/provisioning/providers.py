# 클라우드마다 달라지는 값'을 한 곳에 모음.
# 목적지마다 ProviderConfig를 하나씩 만들어 PROVIDERS에 등록.
from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
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
    # target_infra 를 이 목적지에 맞게 바꿈.
    adapt: Callable[[Mapping[str, Any]], tuple[dict[str, Any], list[str]]] | None = None


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
    # [GCP 는 VPN 용 서비스 계정 키를 항상 만들게 한다 (db_migration 이 VPN 을 만들 때 쓴다). Azure 에는 이 변수가 없다.
    forced_variables={"create_vpn_key": True},
)

AZURE_MEMORY_PER_VCPU_MIB = 2048
AZURE_MAX_MEMORY_MIB = 4096
AZURE_CPU_STEP = 0.25


def _number(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ProvisioningError(f"{name} 값이 숫자가 아닙니다") from None


# 조사 결과의 앱 크기를 Azure 가 받는 조합으로 바꾸는 함수
# 조사 결과는 보통 cpu 1, memory 512 처럼 나오는데, Azure 는 vCPU 1개에 메모리 2048 이어야 해서 그대로 넘기면 거부됨.
# 원본이 필요로 하는 만큼을 덮는 가장 작은 조합을 고름. 한도 넘으면 한도 맞추고 알림.
# db 등급은 현재 넘기지 않음 (known_variables 에 없음). Terraform 은 GCP 등급 이름(db-g1-small 등)을 받지 못함.
def azure_adapt(target_infra: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    infra = dict(target_infra)
    cpu, memory = infra.get("cpu"), infra.get("memory")
    if cpu is None and memory is None:
        return infra, []

    wanted_cpu = _number(cpu, "cpu") if cpu is not None else 0.0
    wanted_memory = _number(memory, "memory") if memory is not None else 0.0

    needed = max(wanted_cpu, wanted_memory / AZURE_MEMORY_PER_VCPU_MIB)
    max_cpu = AZURE_MAX_MEMORY_MIB / AZURE_MEMORY_PER_VCPU_MIB
    new_cpu = min(max(math.ceil(needed / AZURE_CPU_STEP) * AZURE_CPU_STEP, AZURE_CPU_STEP), max_cpu)
    new_memory = int(new_cpu * AZURE_MEMORY_PER_VCPU_MIB)
    infra["cpu"], infra["memory"] = new_cpu, new_memory

    notes: list[str] = []
    if needed > max_cpu:
        notes.append(
            f"원본 앱 크기(cpu {cpu}, memory {memory} MiB)가 Azure 한도를 넘어 cpu {new_cpu}, memory {new_memory} MiB 로 맞췄습니다"
        )
    elif (wanted_cpu, wanted_memory) != (new_cpu, new_memory):
        notes.append(
            f"앱 크기를 Azure 규칙에 맞췄습니다: cpu {cpu}, memory {memory} MiB -> cpu {new_cpu}, memory {new_memory} MiB"
        )
    return infra, notes



AZURE = ProviderConfig(
    name="azure",
    terraform_dir=REPO_ROOT / "infra" / "azure",
    terraform_dir_env=("PROVISIONING_TF_DIR_AZURE",),
    known_variables=frozenset(
        {
            "region",
            "resource_group_name",
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
        }
    ),
    default_region="koreacentral",
    # Azure 는 '구독' 단위. Terraform 변수 이름은 subscription_id, 요청에 없으면 환경변수 AZURE_SUBSCRIPTION_ID 를 읽음.
    scope_variable="subscription_id",
    scope_env="AZURE_SUBSCRIPTION_ID",
    scope_label="Azure 구독 ID",

    adapt=azure_adapt,
)

PROVIDERS: dict[str, ProviderConfig] = {GCP.name: GCP, AZURE.name: AZURE}


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
