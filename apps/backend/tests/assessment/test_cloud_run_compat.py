"""ECS 태스크 정의에서 Cloud Run 으로 옮길 수 없는 설정을 걸러내는지 확인."""

import pytest

from app.features.assessment import app as app_part
from app.features.assessment.app import cloud_run_compat
from app.features.assessment.schemas import AssessmentInput

from .fakes import container, session
from .test_plan import _check, _ready_input, run


def _inp() -> AssessmentInput:
    return AssessmentInput(aws_credentials_ref="secret://run-1/source-cloud", aws_region="ap-northeast-2",
                           target_region="asia-northeast3", github_repo_url="https://github.com/SB-Alpha/target-app")


def _task(c: dict, **task) -> dict:
    return {"containerDefinitions": [c], **task}


def test_plain_task_has_no_blockers_and_no_notes():
    c = container()
    blockers, notes = cloud_run_compat(_task(c), c)
    assert blockers == [] and notes == []


@pytest.mark.parametrize("c, task, expected", [
    (dict(privileged=True), {}, "privileged"),
    (dict(linuxParameters={"devices": [{"hostPath": "/dev/fuse"}]}), {}, "장치"),
    (dict(linuxParameters={"capabilities": {"add": ["SYS_ADMIN"]}}), {}, "capability"),
    (dict(portMappings=[{"containerPort": 5000, "protocol": "udp"}]), {}, "UDP"),
    (dict(resourceRequirements=[{"type": "GPU", "value": "1"}]), {}, "GPU"),
    ({}, {"runtimePlatform": {"cpuArchitecture": "ARM64"}}, "ARM64"),
    ({}, {"pidMode": "host"}, "pidMode"),
    (dict(mountPoints=[{"sourceVolume": "data", "containerPath": "/data"}]),
     {"volumes": [{"name": "data", "host": {"sourcePath": "/mnt/data"}}]}, "호스트 경로"),
    (dict(mountPoints=[{"sourceVolume": "data", "containerPath": "/data"}]),
     {"volumes": [{"name": "data", "dockerVolumeConfiguration": {"scope": "shared"}}]}, "Docker 볼륨"),
])
def test_blocking_settings_are_found(c, task, expected):
    cont = container(**c)
    blockers, _ = cloud_run_compat(_task(cont, **task), cont)
    assert len(blockers) == 1 and expected in blockers[0]


def test_task_scoped_pid_and_ipc_are_not_blockers():
    c = container()
    blockers, _ = cloud_run_compat(_task(c, pidMode="task", ipcMode="task"), c)
    assert blockers == []


def test_command_override_other_containers_and_volumes_are_notes_not_blockers():
    cont = container(command=["sh", "-c", "run --token abc123"],
                     mountPoints=[{"sourceVolume": "efs", "containerPath": "/data"}])
    sidecar = container(name="log-router", portMappings=[])
    task = {"containerDefinitions": [cont, sidecar],
            "volumes": [{"name": "efs", "efsVolumeConfiguration": {"fileSystemId": "fs-1"}}]}
    blockers, notes = cloud_run_compat(task, cont)
    assert blockers == [] and len(notes) == 3
    text = " ".join(notes)
    assert "EFS" in text and "command" in text and "log-router" in text
    assert "abc123" not in text  # 명령어 값은 남기지 않음


def test_collect_puts_result_on_facts():
    c = container(privileged=True)
    res = app_part.collect(_inp(), session(c))
    assert res.facts.cloud_run_blockers and "privileged" in res.facts.cloud_run_blockers[0]


def test_blocker_fails_the_check_and_blocks_approval():
    inp = _ready_input()
    inp.inventory.app.cloud_run_blockers = ["UDP 포트 (Cloud Run 은 HTTP 요청만 받음)"]
    plan = run(inp)
    check = _check(plan, "cloud_run_compatible")
    assert check.result == "fail" and "UDP" in check.detail
    assert not plan.approvable and any("Cloud Run 호환" in b for b in plan.blocking)


def test_clean_task_passes_the_check():
    inp = _ready_input()
    inp.inventory.app.cloud_run_blockers = []
    plan = run(inp)
    assert _check(plan, "cloud_run_compatible").result == "pass" and plan.approvable


def test_manual_source_without_task_definition_has_no_check():
    plan = run(_ready_input())  # cloud_run_blockers=None → 태스크 정의를 보지 않음
    assert all(c.id != "cloud_run_compatible" for c in plan.checks)


@pytest.mark.parametrize("field, value, expected", [
    ("cpu_units", 16384, "16 vCPU"),
    ("memory_mib", 65536, "65536 MiB"),
])
def test_size_over_cloud_run_limit_fails(field, value, expected):
    inp = _ready_input()
    setattr(inp.inventory.app, field, value)
    plan = run(inp)
    check = _check(plan, "cloud_run_compatible")
    assert check.result == "fail" and expected in check.detail


def test_notes_become_medium_risks():
    inp = _ready_input()
    inp.inventory.app.cloud_run_blockers = []
    inp.inventory.app.cloud_run_notes = ["함께 도는 컨테이너 1개(log-router)는 옮기지 않습니다."]
    plan = run(inp)
    risk = next(r for r in plan.risks if r.title == "Cloud Run 에서 확인할 ECS 설정")
    assert risk.level == "medium" and "log-router" in risk.detail
    assert plan.approvable  # 확인 사항은 승인을 막지 않음
