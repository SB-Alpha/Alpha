"""원본 앱(ECS·ECR) 조회. (이주호)

원본은 읽기만 합니다. 조회에 실패한 값은 지어내지 않고 None 으로 두고 gaps 에 남깁니다.
필요한 AWS 권한(읽기 전용): sts:GetCallerIdentity, ecs:ListClusters, ecs:ListServices,
ecs:DescribeServices, ecs:DescribeTaskDefinition, ecr:DescribeImages,
elasticloadbalancing:DescribeTargetGroups
"""

from __future__ import annotations

import re
from typing import Any

from .aws import AwsSession, error_code
from .schemas import AppAssessment, AppInfo, AssessmentInput

# 평문 환경 변수인데 이름이 이렇게 생겼으면 비밀로 보고 값을 넘기지 않습니다. `_KEY` 로 끝나는 이름도 비밀로 봅니다.
SECRET_NAME_HINTS = ("PASSWORD", "PASSWD", "PASSPHRASE", "SECRET", "TOKEN", "PRIVATE", "API_KEY", "ACCESS_KEY",
                     "CREDENTIAL")
# 이름이 평범해도 값이 이렇게 생겼으면 비밀입니다. 예: DATABASE_URL=postgres://alpha:<비밀번호>@host/db
SECRET_VALUE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"  # 주소 안의 사용자:비밀번호@
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS 액세스 키
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."  # JWT
    r"|\b(?:sk-ant-|sk_live_|ghp_|github_pat_|xox[abprs]-|AIza)[A-Za-z0-9_-]{10,}"
)

ECR_IMAGE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repo>[^:@]+)(?::(?P<tag>[^@]+))?$"
)
# 경로만 가져옵니다. ?쿼리·#조각은 토큰이 들어 있을 수 있어 결과·AI 프롬프트로 보내지 않음
URL_PATH = re.compile(r"https?://[^/\s'\"]+(/[^\s'\"|;&?#]*)")


def looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS) or upper == "KEY" or upper.endswith("_KEY")


def has_secret_value(value: str) -> bool:
    return bool(SECRET_VALUE.search(value or ""))


def collect(inp: AssessmentInput, session: AwsSession) -> AppAssessment:
    gaps: list[str] = []

    def call(what: str, fn, **kwargs) -> Any:
        try:
            return fn(**kwargs)
        except Exception as e:  # botocore 예외 포함. 메시지 대신 오류 코드만 남김
            gaps.append(f"{what} 실패 ({error_code(e)})")
            return None

    # 1. 계정
    ident = call("AWS 계정 확인(sts:GetCallerIdentity)", session.client("sts").get_caller_identity)
    account_id = ident.get("Account") if ident else None

    # 2. ECS 서비스 찾기
    ecs = session.client("ecs")
    cluster = inp.ecs_cluster or _only_one(
        call("ECS 클러스터 목록 조회", ecs.list_clusters), "clusterArns", "ECS 클러스터", "ecs_cluster", gaps)
    service_ref = None
    if cluster:
        service_ref = inp.ecs_service or _only_one(
            call("ECS 서비스 목록 조회", ecs.list_services, cluster=cluster),
            "serviceArns", "ECS 서비스", "ecs_service", gaps)

    service = None
    if cluster and service_ref:
        res = call("ECS 서비스 조회", ecs.describe_services, cluster=cluster, services=[service_ref])
        services = (res or {}).get("services") or []
        if services:
            service = services[0]
        elif res is not None:
            gaps.append(f"ECS 서비스 {service_ref} 를 찾지 못함")

    service_name = (service or {}).get("serviceName") or fallback_name(inp)
    if service is None:
        gaps.append("ECS 서비스 미확인: 서비스 이름은 입력값으로 대신함")
        return AppAssessment(facts=AppInfo(service_name=service_name), account_id=account_id, gaps=gaps)

    # 3. 태스크 정의
    task = (call("ECS 태스크 정의 조회", ecs.describe_task_definition,
                 taskDefinition=service["taskDefinition"]) or {}).get("taskDefinition")
    if not task:
        return AppAssessment(facts=AppInfo(service_name=service_name), account_id=account_id, gaps=gaps)

    container = _pick_container(task, service)
    image = container.get("image")
    ports = container.get("portMappings") or []
    container_port = _service_port(service, container, ports, gaps)

    cpu_units = _to_int(task.get("cpu")) or _to_int(container.get("cpu")) or None
    memory_mib = (_to_int(task.get("memory")) or _to_int(container.get("memory"))
                  or _to_int(container.get("memoryReservation")) or None)

    # 4. 환경 변수 / 비밀 참조
    env_vars: dict[str, str] = {}
    for item in container.get("environment") or []:
        name, value = item.get("name"), item.get("value", "")
        if not name:
            continue
        if looks_secret(name):
            gaps.append(f"평문 환경 변수 {name} 가 비밀로 보여 값을 넘기지 않음 (Secrets Manager 로 옮겨야 함)")
            continue
        if has_secret_value(value):  # 값은 gaps 에도 남기지 않음
            gaps.append(f"평문 환경 변수 {name} 의 값에 비밀(접속 정보·키)이 들어 있어 값을 넘기지 않음 "
                        "(Secrets Manager 로 옮겨야 함)")
            continue
        env_vars[name] = value
    secret_refs = {s["name"]: s.get("valueFrom", "") for s in container.get("secrets") or [] if s.get("name")}

    db_user = env_vars.get("PGUSER")
    if db_user is None:
        gaps.append("앱 DB 사용자(PGUSER) 미확인" + (" (비밀로 들어 있음)" if "PGUSER" in secret_refs else ""))

    # 5. 상태 확인 경로: 컨테이너 healthCheck → 로드밸런서 대상 그룹 순서
    health_path = _health_from_container(container)
    if health_path is None:
        health_path = _health_from_target_group(service, session, call)
    if health_path is None:
        gaps.append("상태 확인 경로 미확인 (컨테이너 healthCheck·대상 그룹 모두 없음)")

    # 6. 이미지 digest: 지금 실행 중인 태스크의 digest 가 우선. 없으면 태그로 ECR 조회
    running = _running_digests(ecs, cluster, service, container.get("name"), call)
    if len(running) == 1:
        digest = running[0]
    elif len(running) > 1:
        digest = None
        gaps.append(f"실행 중 태스크 이미지가 {len(running)}개 버전이라 하나로 고정하지 않음 (배포가 끝난 뒤 다시 조사)")
    else:
        digest = _image_digest(image, session, call, gaps)

    blockers, notes = cloud_run_compat(task, container)

    facts = AppInfo(
        service_name=service_name,
        image_uri=image,
        image_digest=digest,
        container_port=container_port,
        cpu_units=cpu_units,
        memory_mib=memory_mib,
        health_check_path=health_path,
        env_vars=env_vars,
        secret_refs=secret_refs,
        cloud_run_blockers=blockers,
        cloud_run_notes=notes,
    )
    return AppAssessment(facts=facts, account_id=account_id, db_user=db_user, gaps=gaps)


def cloud_run_compat(task: dict, container: dict) -> tuple[list[str], list[str]]:
    """태스크 정의에서 Cloud Run 으로 옮길 수 없는 설정(blockers)과 확인이 필요한 설정(notes)을 찾습니다.

    Cloud Run 은 HTTP 요청을 받는 x86-64 컨테이너이고, 호스트(VM)나 장치에 접근할 수 없습니다.
    값은 읽지 않고 설정 이름만 남깁니다 (명령어·경로에 비밀이 있을 수 있음).
    """
    blockers: list[str] = []
    notes: list[str] = []

    if (container.get("privileged") is True):
        blockers.append("privileged 컨테이너 (Cloud Run 은 특권 모드를 지원하지 않음)")
    linux = container.get("linuxParameters") or {}
    if linux.get("devices"):
        blockers.append("호스트 장치 연결 (linuxParameters.devices)")
    if (linux.get("capabilities") or {}).get("add"):
        blockers.append("리눅스 capability 추가 (linuxParameters.capabilities.add)")
    if any(str(p.get("protocol", "tcp")).lower() == "udp" for p in container.get("portMappings") or []):
        blockers.append("UDP 포트 (Cloud Run 은 HTTP 요청만 받음)")
    if any(r.get("type") == "GPU" for r in container.get("resourceRequirements") or []) or             task.get("inferenceAccelerators"):
        blockers.append("GPU·추론 가속기 (이번 매핑은 GPU 를 다루지 않음)")
    if str((task.get("runtimePlatform") or {}).get("cpuArchitecture", "")).upper() == "ARM64":
        blockers.append("ARM64 이미지 (Cloud Run 은 x86-64 만 지원)")
    for mode in ("pidMode", "ipcMode"):
        if task.get(mode) == "host":  # "task" 는 같은 태스크 안 컨테이너끼리라 컨테이너 하나면 문제 없음
            blockers.append(f"{mode}=host (호스트 VM 과 프로세스·IPC 를 공유)")

    volumes = {v.get("name"): v for v in task.get("volumes") or []}
    for mount in container.get("mountPoints") or []:
        vol = volumes.get(mount.get("sourceVolume")) or {}
        if (vol.get("host") or {}).get("sourcePath"):
            blockers.append(f"호스트 경로 마운트 (볼륨 {vol.get('name')})")
        elif vol.get("dockerVolumeConfiguration"):
            blockers.append(f"Docker 볼륨 마운트 (볼륨 {vol.get('name')})")
        elif vol.get("efsVolumeConfiguration"):
            notes.append(f"EFS 볼륨 {vol.get('name')} 를 쓰고 있습니다. Cloud Run 매핑에는 파일 시스템 연결이 없어 "
                         "데이터를 어떻게 옮길지 정해야 합니다.")
        else:
            notes.append(f"볼륨 {vol.get('name') or mount.get('sourceVolume')} 를 마운트합니다. "
                         "Cloud Run 은 재시작하면 컨테이너 파일이 사라집니다.")

    if container.get("command") or container.get("entryPoint"):
        notes.append("태스크 정의가 이미지의 시작 명령(command·entryPoint)을 바꿔 둡니다. "
                     "Cloud Run 매핑은 이 값을 옮기지 않아 이미지 기본 명령으로 시작합니다.")
    others = [c.get("name") for c in task.get("containerDefinitions") or [] if c is not container and c.get("name")]
    if others:
        notes.append(f"함께 도는 컨테이너 {len(others)}개({', '.join(others)})는 옮기지 않습니다. "
                     "앱이 이들에 의존하면 동작하지 않습니다.")
    if task.get("proxyConfiguration"):
        notes.append("App Mesh 프록시 설정이 있습니다. Cloud Run 에는 같은 구성이 없습니다.")

    return blockers, notes


def _only_one(res: dict | None, key: str, what: str, input_name: str, gaps: list[str]) -> str | None:
    if res is None:
        return None
    items = res.get(key) or []
    if len(items) == 1:
        return items[0]
    if not items:
        gaps.append(f"{what} 없음")
    else:
        gaps.append(f"{what} 가 {len(items)}개라 고를 수 없음: 입력 {input_name} 지정 필요")
    return None


def fallback_name(inp: AssessmentInput) -> str:
    if inp.ecs_service:
        return inp.ecs_service.rsplit("/", 1)[-1]
    if inp.github_repo_url:
        return inp.github_repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return "미제공"


def _pick_container(task: dict, service: dict) -> dict:
    containers = task.get("containerDefinitions") or [{}]
    lb_names = {lb.get("containerName") for lb in service.get("loadBalancers") or []}
    for c in containers:
        if c.get("name") in lb_names:
            return c
    for c in containers:
        if c.get("portMappings") and c.get("essential", True):
            return c
    return containers[0]


def _service_port(service: dict, container: dict, ports: list, gaps: list[str]) -> int | None:
    """로드밸런서가 이 컨테이너에 연결한 포트 → 없으면 첫 portMapping."""
    for lb in service.get("loadBalancers") or []:
        if lb.get("containerName") == container.get("name") and lb.get("containerPort"):
            return _to_int(lb["containerPort"])
    if not ports:
        gaps.append("컨테이너 포트 미확인 (portMappings 없음)")
        return None
    if len(ports) > 1:
        gaps.append(f"포트가 {len(ports)}개인데 로드밸런서 연결이 없어 첫 번째 포트를 씀 (확인 필요)")
    return _to_int(ports[0].get("containerPort"))


def _running_digests(ecs, cluster: str | None, service: dict, container_name: str | None, call) -> list[str]:
    """서비스의 RUNNING 태스크에서 이 컨테이너가 실제로 돌리는 이미지 digest 목록 (중복 제거, 정렬)."""
    if not cluster or not container_name:
        return []
    listed = call("ECS 실행 중 태스크 조회", ecs.list_tasks, cluster=cluster,
                  serviceName=service.get("serviceName"), desiredStatus="RUNNING")
    arns = (listed or {}).get("taskArns") or []
    if not arns:
        return []
    described = call("ECS 태스크 상세 조회", ecs.describe_tasks, cluster=cluster, tasks=arns[:100])
    found = {
        c["imageDigest"]
        for t in (described or {}).get("tasks") or []
        for c in t.get("containers") or []
        if c.get("name") == container_name and c.get("imageDigest")
    }
    return sorted(found)


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _health_from_container(container: dict) -> str | None:
    command = " ".join((container.get("healthCheck") or {}).get("command") or [])
    m = URL_PATH.search(command)
    return m.group(1) if m else None


def _health_from_target_group(service: dict, session: AwsSession, call) -> str | None:
    arns = [lb["targetGroupArn"] for lb in service.get("loadBalancers") or [] if lb.get("targetGroupArn")]
    if not arns:
        return None
    res = call("로드밸런서 대상 그룹 조회", session.client("elbv2").describe_target_groups, TargetGroupArns=arns[:1])
    groups = (res or {}).get("TargetGroups") or []
    return groups[0].get("HealthCheckPath") if groups else None


def _image_digest(image: str | None, session: AwsSession, call, gaps: list[str]) -> str | None:
    if not image:
        gaps.append("컨테이너 이미지 주소 미확인")
        return None
    if "@sha256:" in image:
        return image.split("@", 1)[1]
    m = ECR_IMAGE.match(image)
    if not m:
        gaps.append("이미지가 ECR 주소가 아니라 digest 를 조회하지 않음")
        return None
    ecr = session.client("ecr", region_name=m["region"])
    res = call("ECR 이미지 조회", ecr.describe_images, registryId=m["account"], repositoryName=m["repo"],
               imageIds=[{"imageTag": m["tag"] or "latest"}])
    details = (res or {}).get("imageDetails") or []
    return details[0].get("imageDigest") if details else None
