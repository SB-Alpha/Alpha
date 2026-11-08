"""목적지 클라우드별 호환 규칙과 이름. (이주호)

원본에서 읽은 컨테이너 설정은 클라우드 중립 코드(SETTING_LABELS 의 키)로 남기고,
옮길 수 있는지는 여기서 목적지마다 따로 판정합니다.

규칙 값의 뜻:
- 문자열: 이 목적지로는 옮길 수 없음 (그 이유)
- None: 옮길 수 있음
- 규칙에 없는 코드: 확인하지 못함 → 결과 unknown (모르면 통과시키지 않음)

근거:
- Cloud Run: 기존 검사 (x86-64 만, HTTP 요청만, 호스트 접근 불가)
- ECS Fargate: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html
  (privileged·ipcMode·gpu 사용 불가, capability 는 SYS_PTRACE 만 추가 가능, devices 미지원,
  dockerVolumeConfiguration 미지원) 와 task_definition_parameters (Linux pidMode 는 task 만,
  sourcePath 는 EC2 에서만)
- Azure Container Apps: https://learn.microsoft.com/azure/container-apps/containers
  (privileged 불가, linux/amd64 이미지만)
- 크기 한도는 우리 매핑이 실제로 만드는 최대 크기. AWS·Azure 는 provisioning/providers.py 의
  FARGATE_SIZES · AZURE_MAX_MEMORY_MIB 와 같아야 합니다 (tests/assessment/test_targets.py 가 비교).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 원본 설정 코드 → 사람이 읽는 이름. "코드:부가정보" 형태면 부가정보(볼륨 이름 등)를 괄호로 붙입니다.
SETTING_LABELS: dict[str, str] = {
    "privileged": "privileged 컨테이너",
    "host_devices": "호스트 장치 연결 (linuxParameters.devices)",
    "add_capabilities": "리눅스 capability 추가 (SYS_PTRACE 외)",
    "add_sys_ptrace": "리눅스 capability SYS_PTRACE 추가",
    "udp_port": "UDP 포트",
    "tcp_ingress": "TCP 인그레스",
    "no_ingress": "인그레스 없음 (요청을 받지 않는 내부 앱)",
    "gpu": "GPU·추론 가속기",
    "arm64": "ARM64 이미지",
    "pid_host": "pidMode=host (호스트 VM 과 프로세스 공유)",
    "ipc_host": "ipcMode=host (호스트 VM 과 IPC 공유)",
    "host_path_volume": "호스트 경로 마운트",
    "docker_volume": "Docker 볼륨 마운트",
}

_NO_GPU = "이번 매핑은 GPU 를 다루지 않음"
_HTTP_ONLY = "이번 매핑은 HTTP 포트 하나만 엶"
_NO_HOST = "호스트(VM)에 접근할 수 없음"


@dataclass(frozen=True)
class TargetProfile:
    cloud: str
    label: str  # 예: "GCP"
    app_platform: str  # 예: "Cloud Run"
    db_service: str  # 예: "Cloud SQL"
    registry: str
    secret_store: str
    max_vcpu: float | None  # None = 모름
    max_memory_mib: int | None
    rules: dict[str, str | None] = field(default_factory=dict)


PROFILES: dict[str, TargetProfile] = {
    "onprem": TargetProfile(
        cloud="onprem", label="온프렘", app_platform="Docker WAS", db_service="PostgreSQL container",
        registry="ECR archive", secret_store="Local encrypted secrets",
        max_vcpu=None, max_memory_mib=512,
        rules={
            "privileged": "온프렘 어댑터는 특권 컨테이너를 만들지 않음",
            "host_devices": _NO_HOST, "add_capabilities": _NO_HOST, "add_sys_ptrace": _NO_HOST,
            "udp_port": _HTTP_ONLY, "tcp_ingress": _HTTP_ONLY,
            "no_ingress": "Worker가 호출할 HTTP 앱 경로가 필요함",
            "gpu": _NO_GPU, "arm64": None, "pid_host": _NO_HOST, "ipc_host": _NO_HOST,
            "host_path_volume": "WAS의 호스트 볼륨 이전은 지원하지 않음",
            "docker_volume": "WAS의 Docker 볼륨 이전은 지원하지 않음",
        },
    ),
    "gcp": TargetProfile(
        cloud="gcp", label="GCP", app_platform="Cloud Run", db_service="Cloud SQL",
        registry="Artifact Registry", secret_store="GCP Secret Manager",
        max_vcpu=8, max_memory_mib=32768,  # Cloud Run 인스턴스 한 개의 최대 크기
        rules={
            "privileged": "특권 모드를 지원하지 않음",
            "host_devices": _NO_HOST,
            "add_capabilities": "capability 를 추가할 수 없음",
            "add_sys_ptrace": "capability 를 추가할 수 없음",
            "udp_port": "HTTP 요청만 받음",
            "tcp_ingress": "HTTP 요청만 받음",
            "no_ingress": "요청을 받지 않는 앱은 Cloud Run 서비스로 옮길 수 없음",
            "gpu": _NO_GPU,
            "arm64": "x86-64 만 지원",
            "pid_host": _NO_HOST,
            "ipc_host": _NO_HOST,
            "host_path_volume": _NO_HOST,
            "docker_volume": "Docker 볼륨이 없음",
        },
    ),
    "aws": TargetProfile(
        cloud="aws", label="AWS", app_platform="ECS Fargate", db_service="RDS",
        registry="ECR", secret_store="AWS Secrets Manager",
        max_vcpu=4, max_memory_mib=30720,  # provisioning FARGATE_SIZES 의 가장 큰 조합
        rules={
            "privileged": "Fargate 는 privileged 를 받지 않음",
            "host_devices": "Fargate 는 devices 를 지원하지 않음",
            "add_capabilities": "Fargate 는 SYS_PTRACE 만 추가할 수 있음",
            "add_sys_ptrace": None,
            "udp_port": _HTTP_ONLY + " (ALB 뒤에 둠)",
            "tcp_ingress": _HTTP_ONLY + " (ALB 뒤에 둠)",
            "no_ingress": "이번 매핑은 ALB 로 요청을 받는 서비스로 만듦",
            "gpu": _NO_GPU,
            "arm64": "이번 매핑은 x86-64 태스크로 만듦",
            "pid_host": "Fargate 리눅스 태스크는 pidMode=task 만 가능",
            "ipc_host": "Fargate 는 ipcMode 를 받지 않음",
            "host_path_volume": "Fargate 는 호스트 경로(sourcePath)를 받지 않음",
            "docker_volume": "Fargate 는 Docker 볼륨을 지원하지 않음",
        },
    ),
    "azure": TargetProfile(
        cloud="azure", label="Azure", app_platform="Azure Container Apps", db_service="PostgreSQL Flexible Server",
        registry="ACR", secret_store="Azure Key Vault",
        max_vcpu=2, max_memory_mib=4096,  # Consumption 전용 환경 최대 (provisioning AZURE_MAX_MEMORY_MIB)
        rules={
            "privileged": "Container Apps 는 privileged 를 허용하지 않음",
            "udp_port": _HTTP_ONLY + " (HTTP 인그레스)",
            "tcp_ingress": _HTTP_ONLY + " (HTTP 인그레스)",
            "no_ingress": "이번 매핑은 HTTP 인그레스가 있는 앱으로 만듦",
            "gpu": _NO_GPU,
            "arm64": "linux/amd64 이미지만 받음",
            # 장치·capability·pid/ipc·호스트 경로·Docker 볼륨은 문서로 확인하지 못해 unknown 으로 둡니다
        },
    ),
}


def profile(cloud: str) -> TargetProfile | None:
    """모르는 목적지(예: ncp)는 None. 그러면 호환 검사는 unknown 이 됩니다."""
    return PROFILES.get(cloud)


def setting_label(setting: str) -> str:
    code, _, extra = setting.partition(":")
    label = SETTING_LABELS.get(code, code)
    return f"{label} ({extra})" if extra else label


def judge(settings: list[str], cpu_vcpu: float | None, memory_mib: int | None,
          target: TargetProfile) -> tuple[list[str], list[str]]:
    """(옮길 수 없는 이유들, 확인하지 못한 설정들)."""
    blocked: list[str] = []
    unknown: list[str] = []
    for s in settings:
        code = s.partition(":")[0]
        if code not in target.rules:
            unknown.append(setting_label(s))
        elif target.rules[code] is not None:
            blocked.append(f"{setting_label(s)} — {target.rules[code]}")
    if cpu_vcpu is not None and target.max_vcpu is not None and cpu_vcpu > target.max_vcpu:
        blocked.append(f"CPU {cpu_vcpu:g} vCPU ({target.app_platform} 최대 {target.max_vcpu:g} vCPU)")
    if memory_mib is not None and target.max_memory_mib is not None and memory_mib > target.max_memory_mib:
        blocked.append(f"메모리 {memory_mib} MiB ({target.app_platform} 최대 {target.max_memory_mib} MiB)")
    return blocked, unknown
