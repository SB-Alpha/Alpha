import json
import re
from pathlib import Path

import pytest

from app.core.secrets import resolve
from app.features.provisioning.providers import (
    AWS,
    FARGATE_SIZES,
    LIVENESS_PATH,
    READINESS_PATH,
    aws_adapt,
    get_provider,
    terraform_dir,
)
from app.features.provisioning.schemas import AppDeployInput, ProvisioningInput
from app.features.provisioning.service import build_tfvars, deploy_app, resolve_project_id, run
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FakeContext, FakeTerraform, fake_clock

AWS_FIXTURE = Path(__file__).parent / "fixtures" / "aws_output.sample.json"
ADMIN_PASSWORD = "FAKE-AWS-ADMIN-PASSWORD-DO-NOT-LEAK"
ACCOUNT = "123456789012"


def aws_outputs() -> dict:
    return json.loads(AWS_FIXTURE.read_text(encoding="utf-8"))


def declared_variables(folder: Path) -> set[str]:
    text = (folder / "variables.tf").read_text(encoding="utf-8")
    return set(re.findall(r'^variable "([a-z_0-9]+)"', text, re.MULTILINE))


def aws_input(**overrides) -> ProvisioningInput:
    args = {
        "run_id": "mig-aws-001",
        "provider": "aws",
        "project_id": ACCOUNT,
        "target_infra": {
            "region": "ap-northeast-2",
            "service_name": "target-app",
            "cpu": 1,
            "memory": 512,
            "db_name": "postgres",
            "db_tier": "db-g1-small",  # GCP 등급 이름. AWS 로 넘어가면 안 된다
            "db_user": "app",
            "db_logical_decoding": True,
            "db_disk_size_gb": 20,
        },
    }
    args.update(overrides)
    return ProvisioningInput(**args)


# ---------------------------------------------------------------------------
# 설정이 실제 Terraform 과 어긋나지 않는다
# ---------------------------------------------------------------------------


def test_every_variable_we_pass_is_declared_by_the_aws_terraform_folder():
    declared = declared_variables(AWS.terraform_dir)

    assert AWS.known_variables <= declared, sorted(AWS.known_variables - declared)
    assert AWS.scope_variable in declared
    assert set(AWS.forced_variables) <= declared


def test_the_cpu_values_in_code_are_the_ones_the_terraform_accepts():
    # infra/terraform/aws 의 cpu 검증 목록이 바뀌면 이 테스트가 실패해서 FARGATE_SIZES 도 같이 고치게 한다.
    text = (AWS.terraform_dir / "variables.tf").read_text(encoding="utf-8")
    match = re.search(r"contains\(\[([0-9.,\s]+)\],\s*var\.cpu\)", text)
    assert match, "infra/terraform/aws/variables.tf 에서 cpu 검증 목록을 찾지 못했습니다"
    accepted = {float(number) for number in match.group(1).split(",") if number.strip()}

    assert {vcpu for vcpu, _ in FARGATE_SIZES} == accepted


def test_aws_config_values():
    assert AWS.terraform_dir == get_provider("aws").terraform_dir
    assert (AWS.terraform_dir / "main.tf").is_file()
    assert (AWS.default_region, AWS.scope_variable, AWS.scope_env) == (
        "ap-northeast-2", "aws_account_id", "AWS_ACCOUNT_ID",
    )
    assert dict(AWS.forced_variables) == {}  # create_vpn_key 는 GCP 전용


def test_gcp_only_variables_are_not_passed_to_aws():
    for name in ("db_tier", "db_user", "db_logical_decoding", "db_disk_size_gb", "project_id", "create_vpn_key"):
        assert name not in AWS.known_variables


def test_aws_folder_can_be_overridden_by_its_own_variable(monkeypatch):
    monkeypatch.delenv("PROVISIONING_TF_DIR", raising=False)
    monkeypatch.setenv("PROVISIONING_TF_DIR_AWS", "/custom/aws")

    assert terraform_dir(AWS) == Path("/custom/aws")


def test_the_gcp_only_folder_variable_does_not_point_aws_at_the_gcp_folder(monkeypatch):
    monkeypatch.setenv("PROVISIONING_TF_DIR", "/legacy/gcp")
    monkeypatch.delenv("PROVISIONING_TF_DIR_AWS", raising=False)

    assert terraform_dir(AWS) == AWS.terraform_dir


# ---------------------------------------------------------------------------
# 계정 ID 는 필수다
# ---------------------------------------------------------------------------


def test_a_missing_account_id_stops_before_anything_runs_and_names_the_variable(monkeypatch):
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)

    with pytest.raises(ProvisioningError, match="AWS_ACCOUNT_ID"):
        resolve_project_id(None, "aws")


def test_the_request_value_wins_over_the_environment_variable(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "999999999999")

    assert resolve_project_id(ACCOUNT, "aws") == ACCOUNT
    assert resolve_project_id(None, "aws") == "999999999999"


def test_the_account_id_goes_to_terraform_as_aws_account_id_so_a_wrong_login_is_refused():
    values = build_tfvars({"service_name": "target-app"}, project_id=ACCOUNT, deploy_app=False, provider="aws")

    assert values == {"service_name": "target-app", "aws_account_id": ACCOUNT, "deploy_app": False}


# ---------------------------------------------------------------------------
# 앱 크기 번역 (Fargate 가 받는 조합)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpu", "memory", "expected"),
    [
        (1, 512, (1.0, 2048)),  # 1 vCPU 는 메모리가 최소 2048
        (0.25, 512, (0.25, 512)),  # 이미 가능한 조합
        (0.25, 1500, (0.25, 2048)),
        (0.5, 4096, (0.5, 4096)),
        (0.5, 4097, (1.0, 5120)),  # 0.5 vCPU 의 최대 메모리를 넘으면 vCPU 를 올린다
        (1, 9000, (2.0, 9216)),
        (2, None, (2.0, 4096)),  # cpu 만 있으면 그 cpu 의 가장 작은 메모리
        (None, 3000, (0.5, 3072)),  # 메모리만 있으면 그것을 덮는 가장 작은 vCPU
        (None, 20000, (4.0, 20480)),
    ],
)
def test_app_size_is_translated_to_a_combination_fargate_accepts(cpu, memory, expected):
    infra, _ = aws_adapt({"cpu": cpu, "memory": memory})

    assert (infra["cpu"], infra["memory"]) == expected
    assert infra["memory"] in dict(FARGATE_SIZES)[infra["cpu"]]  # 실제로 가능한 조합이다


def test_translation_explains_what_it_changed_and_what_it_capped():
    _, changed = aws_adapt({"cpu": 1, "memory": 512})
    _, same = aws_adapt({"cpu": 0.25, "memory": 512})
    capped_infra, capped = aws_adapt({"cpu": 8, "memory": 1024})

    assert changed and "Fargate 규칙에 맞췄습니다" in changed[0]
    assert same == []
    assert (capped_infra["cpu"], capped_infra["memory"]) == (4.0, 8192)  # cpu 만 넘었으니 메모리는 4 vCPU 의 최솟값
    assert capped and "한도를 넘어" in capped[0]


@pytest.mark.parametrize(
    ("cpu", "memory", "expected"),
    [
        (8, 512, (4.0, 8192)),  # cpu 만 한도를 넘음: cpu 는 4, 메모리는 그 cpu 의 최솟값 (최대로 올리지 않음)
        (8, 20000, (4.0, 20480)),  # 메모리는 요청한 값을 덮는 가장 작은 값
        (2, 99999, (4.0, 30720)),  # 메모리만 한도를 넘음: 메모리를 한도로, 그러려면 4 vCPU 가 필요함
        (16, 99999, (4.0, 30720)),  # 둘 다 넘음
    ],
)
def test_only_the_axis_over_the_limit_is_capped(cpu, memory, expected):
    infra, notes = aws_adapt({"cpu": cpu, "memory": memory})

    assert (infra["cpu"], infra["memory"]) == expected
    assert infra["memory"] in dict(FARGATE_SIZES)[infra["cpu"]]
    assert notes and "한도를 넘어" in notes[0]


def test_unknown_size_is_left_to_the_terraform_defaults():
    infra, notes = aws_adapt({"service_name": "target-app"})

    assert infra == {"service_name": "target-app"} and notes == []


def test_a_non_numeric_size_is_an_error():
    with pytest.raises(ProvisioningError, match="cpu"):
        aws_adapt({"cpu": "많이"})


def test_translation_does_not_modify_the_callers_dictionary():
    original = {"cpu": 1, "memory": 512}

    aws_adapt(original)

    assert original == {"cpu": 1, "memory": 512}


# ---------------------------------------------------------------------------
# provisioning 전체 흐름
# ---------------------------------------------------------------------------


def test_aws_provisioning_writes_translated_variables_and_builds_the_target():
    tf, ctx = FakeTerraform(outputs=aws_outputs()), FakeContext()
    result = run(aws_input(), ctx, terraform=tf)

    assert tf.var_values == {
        "region": "ap-northeast-2",
        "service_name": "target-app",
        "db_name": "postgres",
        "cpu": 1.0,
        "memory": 2048,
        "aws_account_id": ACCOUNT,
        "deploy_app": False,
    }
    assert result.target["db"]["private_ip"].endswith(".rds.amazonaws.com")  # IP 가 아니라 호스트 이름이다
    assert result.target["network"]["vpc_name"] == "target-app"
    assert result.target["project_id"] == ACCOUNT
    assert any("앱 크기를 Fargate 규칙에 맞췄습니다" in (message or "") for _, _, message in ctx.calls)


def test_aws_target_passes_the_cdc_checks_except_for_the_missing_vpn_key():
    # ssl_mode, PostgreSQL 16, 상대 대역(OCI 10.50, 원본 AWS 172.31)과 겹치지 않음
    result = run(aws_input(), FakeContext(), terraform=FakeTerraform(outputs=aws_outputs()))

    assert result.problems and all("target-cloud" in problem for problem in result.problems)


def test_aws_provisioning_stores_the_master_user_as_the_admin_secret():
    result = run(aws_input(), FakeContext(), terraform=FakeTerraform(outputs=aws_outputs()))

    refs = result.target["credentials_ref"]
    assert resolve(refs["admin"]) == {"user": "alpha", "password": ADMIN_PASSWORD}
    with pytest.raises(KeyError):
        resolve(refs["cloud"])  # AWS 는 VPN 자격 증명을 만들지 않는다
    assert ADMIN_PASSWORD not in result.model_dump_json()


def test_aws_region_default_applies_when_the_plan_has_none():
    result = run(
        aws_input(target_infra={"service_name": "target-app"}), FakeContext(), terraform=FakeTerraform(outputs=aws_outputs())
    )

    assert result.target["region"] == "ap-northeast-2"


def test_aws_deploy_app_turns_the_app_on_with_the_same_translated_size():
    outputs = aws_outputs()
    outputs["app_url"]["value"] = "http://target-app-123.ap-northeast-2.elb.amazonaws.com"
    tf = FakeTerraform(outputs=outputs)
    result = deploy_app(
        AppDeployInput(
            run_id="mig-aws-001", provider="aws", project_id=ACCOUNT, update_worker_target=False,
            target_infra={"service_name": "target-app", "cpu": 1, "memory": 512, "db_tier": "db-g1-small"},
        ),
        FakeContext(), terraform=tf, http_get=lambda url: 200, sleep=lambda s: None, clock=fake_clock(),
    )

    assert tf.var_values == {
        "service_name": "target-app", "cpu": 1.0, "memory": 2048, "aws_account_id": ACCOUNT, "deploy_app": True,
    }
    assert result.healthy is True and result.app_url == "http://target-app-123.ap-northeast-2.elb.amazonaws.com"


# ---------------------------------------------------------------------------
# ALB 상태 확인 경로는 live (DB 까지 보는 ready 를 쓰면 DB 가 잠깐 막힐 때 정상 태스크가 교체된다)
# ---------------------------------------------------------------------------


def test_the_readiness_path_is_replaced_by_the_liveness_path_for_the_alb():
    infra, notes = aws_adapt({"health_check_path": READINESS_PATH, "cpu": 1, "memory": 2048})

    assert infra["health_check_path"] == LIVENESS_PATH == "/health/live"
    assert any(READINESS_PATH in note and LIVENESS_PATH in note for note in notes)


def test_the_path_is_replaced_even_when_the_plan_has_no_app_size():
    infra, notes = aws_adapt({"health_check_path": READINESS_PATH})

    assert infra == {"health_check_path": LIVENESS_PATH}
    assert len(notes) == 1


@pytest.mark.parametrize("path", ["/health/live", "/healthz", "/api/ping", "/"])
def test_a_path_that_is_not_the_readiness_path_is_left_alone(path):
    infra, notes = aws_adapt({"health_check_path": path})

    assert infra["health_check_path"] == path
    assert notes == []


def test_a_missing_path_is_left_to_the_terraform_default():
    infra, _ = aws_adapt({"cpu": 1, "memory": 2048})

    assert "health_check_path" not in infra


def test_aws_provisioning_sends_the_liveness_path_to_terraform_even_if_the_plan_says_ready():
    tf = FakeTerraform(outputs=aws_outputs())
    infra = {"service_name": "target-app", "health_check_path": READINESS_PATH}

    run(aws_input(target_infra=infra), FakeContext(), terraform=tf)

    assert tf.var_values["health_check_path"] == LIVENESS_PATH


def test_the_terraform_default_path_is_the_same_liveness_path():
    text = (AWS.terraform_dir / "variables.tf").read_text(encoding="utf-8")
    match = re.search(r'variable "health_check_path" \{.*?default\s*=\s*"([^"]+)"', text, re.DOTALL)

    assert match, "infra/terraform/aws/variables.tf 에서 health_check_path 기본값을 찾지 못했습니다"
    assert match.group(1) == LIVENESS_PATH


def test_the_memory_combinations_in_code_are_the_ones_the_terraform_accepts():
    # infra/terraform/aws 의 memory 검증 목록이 바뀌면 이 테스트가 실패해서 FARGATE_SIZES 도 같이 고치게 한다.
    text = (AWS.terraform_dir / "variables.tf").read_text(encoding="utf-8")
    block = text[text.index('variable "memory"'):]
    block = block[: block.index("error_message")]
    accepted = {
        float(vcpu): tuple(int(number) for number in numbers.split(",") if number.strip())
        for vcpu, numbers in re.findall(r'"([0-9.]+)"\s*=\s*\[([0-9,\s]+)\]', block)
    }

    assert accepted, "infra/terraform/aws/variables.tf 에서 memory 검증 목록을 찾지 못했습니다"
    assert accepted == dict(FARGATE_SIZES)


def test_a_size_exactly_at_the_limit_is_not_reported_as_over_it():
    infra, notes = aws_adapt({"cpu": 4, "memory": 30720})

    assert (infra["cpu"], infra["memory"]) == (4.0, 30720)
    assert notes == []
