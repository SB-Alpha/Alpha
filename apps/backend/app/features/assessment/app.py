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

# 평문 환경 변수인데 이름이 이렇게 생겼으면 비밀로 보고 값을 넘기지 않습니다.
SECRET_NAME_HINTS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "PRIVATE_KEY", "API_KEY", "ACCESS_KEY", "CREDENTIAL")

ECR_IMAGE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repo>[^:@]+)(?::(?P<tag>[^@]+))?$"
)
URL_PATH = re.compile(r"https?://[^/\s'\"]+(/[^\s'\"|;&]*)")


def looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


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
    container_port = ports[0].get("containerPort") if ports else None
    if container_port is None:
        gaps.append("컨테이너 포트 미확인 (portMappings 없음)")

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

    # 6. 이미지 digest
    digest = _image_digest(image, session, call, gaps)

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
    )
    return AppAssessment(facts=facts, account_id=account_id, db_user=db_user, gaps=gaps)


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
