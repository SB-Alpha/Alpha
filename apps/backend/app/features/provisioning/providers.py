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


# DB 등급 변환 (클라우드 중립 값 db_memory_gib · db_vcpu 를 목적지 등급으로)
# 원본 인스턴스의 메모리(GiB)를 덮는 가장 작은 등급을 고른다. 크기를 모르면 등급을 넘기지 않아 Terraform 기본값을 쓴다.
# (이름, 메모리 GiB, vCPU), 작은 것부터. 새 등급을 넣을 때는 위 두 CLI 로 이름과 크기를 다시 확인한다.
AZURE_DB_TIERS: tuple[tuple[str, float, int], ...] = (
    ("B_Standard_B1ms", 2, 1),
    ("B_Standard_B2s", 4, 2),
    ("B_Standard_B2ms", 8, 2),
    ("GP_Standard_D4ds_v5", 16, 4),
    ("GP_Standard_D8ds_v5", 32, 8),
    ("GP_Standard_D16ds_v5", 64, 16),
    ("GP_Standard_D32ds_v5", 128, 32),
)
AWS_DB_CLASSES: tuple[tuple[str, float, int], ...] = (
    ("db.t4g.micro", 1, 2),
    ("db.t4g.small", 2, 2),
    ("db.t4g.medium", 4, 2),
    ("db.t4g.large", 8, 2),
    ("db.t4g.xlarge", 16, 4),
    ("db.t4g.2xlarge", 32, 8),
    ("db.m6g.4xlarge", 64, 16),
    ("db.m6g.8xlarge", 128, 32),
)
# infra/azure 의 db_disk_size_gb 검증 목록과 같아야 한다 (가드 테스트가 비교한다)
AZURE_DISK_SIZES_GB = (32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
AWS_MIN_DISK_GB = 20  # gp3 최소 크기


def _pick_db_tier(
    sizes: tuple[tuple[str, float, int], ...], memory_gib: float | None, vcpu: float | None
) -> tuple[str | None, bool]:
    """원본 메모리를 덮는 가장 작은 등급과 '가장 큰 등급으로도 모자랐는지'. 메모리를 모르면 vCPU 로, 둘 다 모르면 (None, False)."""
    if memory_gib is None and vcpu is None:
        return None, False
    for name, mem, cpu in sizes:
        enough = mem >= memory_gib if memory_gib is not None else cpu >= (vcpu or 0)
        if enough:
            return name, False
    return sizes[-1][0], True


def _translate_db(
    infra: dict[str, Any],
    notes: list[str],
    *,
    sizes: tuple[tuple[str, float, int], ...],
    tier_key: str,
    pick_disk: Callable[[float, list[str]], int],
) -> None:
    """infra 를 직접 고친다: 등급(tier_key)과 디스크 크기를 목적지 규칙으로 바꾼다."""
    infra.pop("db_tier", None)  # GCP 등급 이름은 어느 경우에도 넘기지 않는다
    memory = infra.get("db_memory_gib")
    vcpu = infra.get("db_vcpu")
    memory = _number(memory, "db_memory_gib") if memory is not None else None
    vcpu = _number(vcpu, "db_vcpu") if vcpu is not None else None
    tier, capped = _pick_db_tier(sizes, memory, vcpu)
    if tier:
        infra[tier_key] = tier
        source = f"메모리 {memory:g} GiB" if memory is not None else f"vCPU {vcpu:g}개"
        if capped:
            notes.append(f"원본 DB({source})가 가장 큰 등급 {tier} 로도 모자라 그 등급으로 맞췄습니다. 확인이 필요합니다")
        else:
            notes.append(f"DB 등급을 원본({source}) 이상인 가장 작은 등급 {tier} 로 정했습니다")
    disk = infra.get("db_disk_size_gb")
    if disk is not None:
        infra["db_disk_size_gb"] = pick_disk(_number(disk, "db_disk_size_gb"), notes)


def _azure_disk(wanted_gb: float, notes: list[str]) -> int:
    for size in AZURE_DISK_SIZES_GB:
        if size >= wanted_gb:
            return size
    notes.append(f"원본 DB 디스크({wanted_gb:g} GB)가 Azure 최대 {AZURE_DISK_SIZES_GB[-1]} GB 를 넘어 그 크기로 맞췄습니다")
    return AZURE_DISK_SIZES_GB[-1]


def _aws_disk(wanted_gb: float, notes: list[str]) -> int:
    return max(AWS_MIN_DISK_GB, math.ceil(wanted_gb))


# 조사 결과의 앱 크기를 Azure 가 받는 조합으로 바꾸는 함수
# 조사 결과는 보통 cpu 1, memory 512 처럼 나오는데, Azure 는 vCPU 1개에 메모리 2048 이어야 해서 그대로 넘기면 거부됨.
# 원본이 필요로 하는 만큼을 덮는 가장 작은 조합을 고름. 한도 넘으면 한도 맞추고 알림.
# db 등급은 현재 넘기지 않음 (known_variables 에 없음). Terraform 은 GCP 등급 이름(db-g1-small 등)을 받지 못함.
def azure_adapt(target_infra: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    infra = dict(target_infra)
    db_notes: list[str] = []
    _translate_db(infra, db_notes, sizes=AZURE_DB_TIERS, tier_key="db_tier", pick_disk=_azure_disk)
    cpu, memory = infra.get("cpu"), infra.get("memory")
    if cpu is None and memory is None:
        return infra, db_notes

    wanted_cpu = _number(cpu, "cpu") if cpu is not None else 0.0
    wanted_memory = _number(memory, "memory") if memory is not None else 0.0

    needed = max(wanted_cpu, wanted_memory / AZURE_MEMORY_PER_VCPU_MIB)
    max_cpu = AZURE_MAX_MEMORY_MIB / AZURE_MEMORY_PER_VCPU_MIB
    new_cpu = min(max(math.ceil(needed / AZURE_CPU_STEP) * AZURE_CPU_STEP, AZURE_CPU_STEP), max_cpu)
    new_memory = int(new_cpu * AZURE_MEMORY_PER_VCPU_MIB)
    infra["cpu"], infra["memory"] = new_cpu, new_memory

    notes = db_notes
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
            "db_tier",
            "db_disk_size_gb",
        }
    ),
    default_region="koreacentral",
    # Azure 는 '구독' 단위. Terraform 변수 이름은 subscription_id, 요청에 없으면 환경변수 AZURE_SUBSCRIPTION_ID 를 읽음.
    scope_variable="subscription_id",
    scope_env="AZURE_SUBSCRIPTION_ID",
    scope_label="Azure 구독 ID",

    adapt=azure_adapt,
)

# Fargate 가 받는 (vCPU, 메모리 MiB) 조합. vCPU 마다 메모리 범위가 정해져 있다 (1 vCPU = 1024 CPU 단위).
def _mib_range(low: int, high: int) -> tuple[int, ...]:
    return tuple(range(low, high + 1, 1024))


FARGATE_SIZES: tuple[tuple[float, tuple[int, ...]], ...] = (
    (0.25, (512, 1024, 2048)),
    (0.5, _mib_range(1024, 4096)),
    (1.0, _mib_range(2048, 8192)),
    (2.0, _mib_range(4096, 16384)),
    (4.0, _mib_range(8192, 30720)),
)


# ALB 대상 그룹은 프로세스만 보는 경로(live)를 써야 한다. DB 까지 보는 ready 를 쓰면 RDS 재부팅처럼 DB 가 잠깐 막힐 때
# 정상 태스크까지 계속 교체된다. 조사 단계는 원본에서 경로를 못 읽으면 기본값으로 ready 를 채우므로 여기서 바꾼다.
# 전환 판단용 ready 확인은 provisioning 이 앱 배포 뒤에 따로 한다 (service._wait_healthy).
READINESS_PATH = "/health/ready"
LIVENESS_PATH = "/health/live"


# 조사 결과의 앱 크기를 Fargate 가 받는 조합으로 바꾸는 함수. 상태 확인 경로도 여기서 ALB 용으로 바꾼다.
# 원본이 필요로 하는 만큼을 덮는 가장 작은 조합을 고르고, 한도를 넘으면 가장 큰 조합으로 맞추고 알린다.
# DB 등급은 넘기지 않는다 (known_variables 에 없음). 조사 결과의 등급 이름은 GCP 것이라 RDS 가 받지 못한다.
def aws_adapt(target_infra: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    infra = dict(target_infra)
    notes: list[str] = []
    _translate_db(infra, notes, sizes=AWS_DB_CLASSES, tier_key="db_instance_class", pick_disk=_aws_disk)
    if infra.get("health_check_path") == READINESS_PATH:
        infra["health_check_path"] = LIVENESS_PATH
        notes.append(
            f"ALB 상태 확인 경로를 {READINESS_PATH} 에서 {LIVENESS_PATH} 로 바꿨습니다 "
            "(DB 가 잠깐 막혀도 정상 태스크가 교체되지 않게 하려는 것입니다)"
        )
    cpu, memory = infra.get("cpu"), infra.get("memory")
    if cpu is None and memory is None:
        return infra, notes

    wanted_cpu = _number(cpu, "cpu") if cpu is not None else 0.0
    wanted_memory = _number(memory, "memory") if memory is not None else 0.0

    # 한도를 넘는 축만 그 축의 한도로 맞춘다. 예를 들어 cpu 만 넘으면 cpu 는 4 로, 메모리는 요청한 값을 덮는 가장 작은 값으로 한다
    # (한도를 넘었다고 다른 축까지 최대로 올리면 비용이 크게 나온다).
    max_cpu, max_memory = FARGATE_SIZES[-1][0], FARGATE_SIZES[-1][1][-1]
    over_limit = wanted_cpu > max_cpu or wanted_memory > max_memory
    search_cpu, search_memory = min(wanted_cpu, max_cpu), min(wanted_memory, max_memory)

    chosen: tuple[float, int] | None = None
    for vcpu, memories in FARGATE_SIZES:
        if vcpu < search_cpu:
            continue
        fit = next((value for value in memories if value >= search_memory), None)
        if fit is not None:
            chosen = (vcpu, fit)
            break
    assert chosen is not None  # 한도 안으로 줄였으므로 항상 찾는다

    if over_limit:
        notes.append(
            f"원본 앱 크기(cpu {cpu}, memory {memory} MiB)가 Fargate 한도를 넘어 cpu {chosen[0]:g}, memory {chosen[1]} MiB 로 맞췄습니다"
        )
    elif (wanted_cpu, wanted_memory) != chosen:
        notes.append(
            f"앱 크기를 Fargate 규칙에 맞췄습니다: cpu {cpu}, memory {memory} MiB -> cpu {chosen[0]:g}, memory {chosen[1]} MiB"
        )
    infra["cpu"], infra["memory"] = chosen
    return infra, notes


AWS = ProviderConfig(
    name="aws",
    terraform_dir=REPO_ROOT / "infra" / "terraform" / "aws",
    terraform_dir_env=("PROVISIONING_TF_DIR_AWS",),
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
            "db_instance_class",
            "db_disk_size_gb",
        }
    ),
    default_region="ap-northeast-2",
    # AWS 는 '계정' 단위. 자원이 만들어질 계정은 실행 환경의 로그인이 정하고, 이 값은 로그인이 맞는 계정인지 확인하는 데 쓰인다.
    # 그래서 GCP·Azure 와 같이 필수로 둔다 (이 컴퓨터의 기본 AWS 프로필이 다른 서비스 계정일 수 있다).
    scope_variable="aws_account_id",
    scope_env="AWS_ACCOUNT_ID",
    scope_label="AWS 계정 ID",
    adapt=aws_adapt,
)

PROVIDERS: dict[str, ProviderConfig] = {GCP.name: GCP, AZURE.name: AZURE, AWS.name: AWS}


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
