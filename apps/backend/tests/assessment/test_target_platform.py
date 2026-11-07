"""원본 컨테이너 설정을 찾고, 목적지(GCP·AWS·Azure)마다 옮길 수 있는지 따로 판정하는지 확인."""

import pytest

from app.features.assessment import app as app_part
from app.features.assessment import targets
from app.features.assessment.app import container_settings
from app.features.assessment.schemas import AssessmentInput, TargetSelection

from .fakes import container, session
from .test_plan import _check, _ready_input, run

CHECK = "target_platform_compatible"


def _inp() -> AssessmentInput:
    return AssessmentInput(aws_credentials_ref="secret://run-1/source-cloud", aws_region="ap-northeast-2",
                           target_region="asia-northeast3", github_repo_url="https://github.com/SB-Alpha/target-app")


def _task(c: dict, **task) -> dict:
    return {"containerDefinitions": [c], **task}


def _plan(cloud: str, settings: list[str] | None = None, **app):
    inp = _ready_input()
    inp.target = TargetSelection(cloud=cloud, region="r")
    inp.inventory.app.container_settings = settings
    for k, v in app.items():
        setattr(inp.inventory.app, k, v)
    return run(inp)


# --- 원본 설정 찾기 (판정 없이 코드만) -------------------------------------------------


def test_plain_task_has_no_settings_and_no_notes():
    c = container()
    assert container_settings(_task(c), c) == ([], [])


@pytest.mark.parametrize("c, task, expected", [
    (dict(privileged=True), {}, "privileged"),
    (dict(linuxParameters={"devices": [{"hostPath": "/dev/fuse"}]}), {}, "host_devices"),
    (dict(linuxParameters={"capabilities": {"add": ["SYS_ADMIN"]}}), {}, "add_capabilities"),
    (dict(linuxParameters={"capabilities": {"add": ["SYS_PTRACE"]}}), {}, "add_sys_ptrace"),
    (dict(linuxParameters={"capabilities": {"add": ["CAP_SYS_PTRACE", "NET_ADMIN"]}}), {}, "add_capabilities"),
    (dict(portMappings=[{"containerPort": 5000, "protocol": "udp"}]), {}, "udp_port"),
    (dict(resourceRequirements=[{"type": "GPU", "value": "1"}]), {}, "gpu"),
    ({}, {"runtimePlatform": {"cpuArchitecture": "ARM64"}}, "arm64"),
    ({}, {"pidMode": "host"}, "pid_host"),
    ({}, {"ipcMode": "host"}, "ipc_host"),
    (dict(mountPoints=[{"sourceVolume": "data", "containerPath": "/data"}]),
     {"volumes": [{"name": "data", "host": {"sourcePath": "/mnt/data"}}]}, "host_path_volume:data"),
    (dict(mountPoints=[{"sourceVolume": "data", "containerPath": "/data"}]),
     {"volumes": [{"name": "data", "dockerVolumeConfiguration": {"scope": "shared"}}]}, "docker_volume:data"),
])
def test_settings_are_found_as_codes(c, task, expected):
    cont = container(**c)
    settings, _ = container_settings(_task(cont, **task), cont)
    assert settings == [expected]


def test_every_code_has_a_label():
    for code in targets.SETTING_LABELS:
        assert targets.setting_label(code) != code


def test_task_scoped_pid_and_ipc_are_not_settings():
    c = container()
    assert container_settings(_task(c, pidMode="task", ipcMode="task"), c)[0] == []


def test_command_override_other_containers_and_volumes_are_notes():
    cont = container(command=["sh", "-c", "run --token abc123"],
                     mountPoints=[{"sourceVolume": "efs", "containerPath": "/data"}])
    sidecar = container(name="log-router", portMappings=[])
    task = {"containerDefinitions": [cont, sidecar],
            "volumes": [{"name": "efs", "efsVolumeConfiguration": {"fileSystemId": "fs-1"}}]}
    settings, notes = container_settings(task, cont)
    assert settings == [] and len(notes) == 3
    text = " ".join(notes)
    assert "EFS" in text and "command" in text and "log-router" in text
    assert "abc123" not in text  # 명령어 값은 남기지 않음
    assert "Cloud Run" not in text  # 목적지와 상관없는 문구


def test_collect_puts_settings_on_facts():
    res = app_part.collect(_inp(), session(container(privileged=True)))
    assert res.facts.container_settings == ["privileged"]


# --- 목적지별 판정 ---------------------------------------------------------------------


def test_default_target_is_gcp_and_keeps_the_cloud_run_rules():
    inp = _ready_input()
    inp.inventory.app.container_settings = ["udp_port"]
    plan = run(inp)  # target.cloud 기본값 gcp
    check = _check(plan, CHECK)
    assert check.result == "fail" and "Cloud Run" in check.title and "UDP" in check.detail
    assert not plan.approvable and any("Cloud Run" in b for b in plan.blocking)


@pytest.mark.parametrize("cloud", ["gcp", "aws", "azure"])
def test_clean_task_passes_on_every_target(cloud):
    plan = _plan(cloud, [])
    assert _check(plan, CHECK).result == "pass" and plan.approvable


@pytest.mark.parametrize("setting, gcp, aws, azure", [
    ("privileged", "fail", "fail", "fail"),
    ("arm64", "fail", "fail", "fail"),  # AWS 는 Fargate 가 받지만 우리 매핑이 x86-64 태스크로 만듦
    ("add_sys_ptrace", "fail", "pass", "unknown"),  # Fargate 는 SYS_PTRACE 추가만 허용
    ("add_capabilities", "fail", "fail", "unknown"),
    ("udp_port", "fail", "fail", "fail"),
    ("gpu", "fail", "fail", "fail"),
    ("pid_host", "fail", "fail", "unknown"),
    ("host_path_volume:data", "fail", "fail", "unknown"),
    ("docker_volume:data", "fail", "fail", "unknown"),
    ("tcp_ingress", "fail", "fail", "fail"),
])
def test_same_setting_is_judged_per_target(setting, gcp, aws, azure):
    for cloud, expected in (("gcp", gcp), ("aws", aws), ("azure", azure)):
        plan = _plan(cloud, [setting])
        check = _check(plan, CHECK)
        assert check.result == expected, (cloud, check.detail)
        assert plan.approvable is (expected == "pass")  # 모르면 승인 안 됨


def test_aws_target_does_not_mention_cloud_run():
    plan = _plan("aws", ["udp_port"])
    check = _check(plan, CHECK)
    assert "ECS Fargate" in check.title and "Cloud Run" not in check.detail


@pytest.mark.parametrize("cloud, cpu_units, memory_mib, expected", [
    ("gcp", 8 * 1024, 32768, "pass"),
    ("gcp", 16 * 1024, 1024, "fail"),
    ("gcp", 1024, 65536, "fail"),
    ("aws", 4 * 1024, 30720, "pass"),
    ("aws", 8 * 1024, 1024, "fail"),  # Cloud Run 이면 통과하지만 이번 AWS 매핑 최대는 4 vCPU
    ("azure", 2 * 1024, 4096, "pass"),
    ("azure", 1024, 8192, "fail"),
])
def test_size_limit_is_the_targets(cloud, cpu_units, memory_mib, expected):
    plan = _plan(cloud, [], cpu_units=cpu_units, memory_mib=memory_mib)
    assert _check(plan, CHECK).result == expected


def test_size_is_checked_even_without_a_task_definition():
    plan = _plan("azure", None, cpu_units=4096, memory_mib=8192)  # 직접 입력: 설정은 모르지만 크기는 앎
    check = _check(plan, CHECK)
    assert check.result == "fail" and "CPU 4 vCPU" in check.detail and "8192 MiB" in check.detail


def test_manual_source_without_task_definition_has_no_check():
    plan = run(_ready_input())  # container_settings=None, 크기는 한도 안
    assert all(c.id != CHECK for c in plan.checks)


def test_target_without_rules_is_unknown_not_pass():
    plan = _plan("ncp", [])
    check = _check(plan, CHECK)
    assert check.result == "unknown" and not plan.approvable


def test_notes_become_medium_risks_named_after_the_target():
    inp = _ready_input()
    inp.target = TargetSelection(cloud="aws", region="ap-northeast-2")
    inp.inventory.app.container_settings = []
    inp.inventory.app.container_notes = ["함께 도는 컨테이너 1개(log-router)는 옮기지 않습니다."]
    plan = run(inp)
    risk = next(r for r in plan.risks if r.title == "ECS Fargate 에서 확인할 원본 설정")
    assert risk.level == "medium" and "log-router" in risk.detail
    assert plan.approvable  # 확인 사항은 승인을 막지 않음


@pytest.mark.parametrize("cloud, words", [
    ("gcp", ["Cloud Run", "Artifact Registry", "경로를 GCP 로"]),
    ("aws", ["ECS Fargate", "ECR", "RDS", "경로를 AWS 로"]),
    ("azure", ["Azure Container Apps", "ACR", "PostgreSQL Flexible Server", "경로를 Azure 로"]),
])
def test_steps_and_summary_name_the_target(cloud, words):
    plan = _plan(cloud, [])
    text = " ".join(s.action for s in plan.steps) + " " + " ".join(plan.summary)
    for w in words:
        assert w in text, w
    if cloud != "gcp":
        assert "Cloud Run" not in text and "Cloud SQL" not in text


# --- provisioning 과 크기 한도가 같은지 -------------------------------------------------


def test_size_limits_match_what_provisioning_builds():
    from app.features.provisioning import providers

    aws = targets.PROFILES["aws"]
    assert (aws.max_vcpu, aws.max_memory_mib) == (providers.FARGATE_SIZES[-1][0], providers.FARGATE_SIZES[-1][1][-1])
    azure = targets.PROFILES["azure"]
    assert azure.max_memory_mib == providers.AZURE_MAX_MEMORY_MIB
    assert azure.max_vcpu == providers.AZURE_MAX_MEMORY_MIB / providers.AZURE_MEMORY_PER_VCPU_MIB
