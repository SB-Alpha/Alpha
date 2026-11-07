import json
import re
from pathlib import Path

import pytest

from app.core.secrets import resolve
from app.features.provisioning.image_transfer import destination_login
from app.features.provisioning.providers import (
    AZURE,
    AZURE_CPU_STEP,
    AZURE_MAX_MEMORY_MIB,
    AZURE_MEMORY_PER_VCPU_MIB,
    GCP,
    azure_adapt,
    get_provider,
    terraform_dir,
)
from app.features.provisioning.schemas import AppDeployInput, ProvisioningInput
from app.features.provisioning.service import build_tfvars, deploy_app, resolve_project_id, run
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FakeContext, FakeTerraform, fake_clock

AZURE_FIXTURE = Path(__file__).parent / "fixtures" / "azure_output.sample.json"
ADMIN_PASSWORD = "FAKE-AZURE-ADMIN-PASSWORD-DO-NOT-LEAK"


def azure_outputs() -> dict:
    return json.loads(AZURE_FIXTURE.read_text(encoding="utf-8"))


def declared_variables(folder: Path) -> set[str]:
    text = (folder / "variables.tf").read_text(encoding="utf-8")
    return set(re.findall(r'^variable "([a-z_0-9]+)"', text, re.MULTILINE))


def azure_input(**overrides) -> ProvisioningInput:
    args = {
        "run_id": "mig-azure-001",
        "provider": "azure",
        "project_id": "00000000-0000-0000-0000-000000000000",
        "target_infra": {
            "region": "koreacentral",
            "service_name": "target-app",
            "cpu": 1,
            "memory": 512,
            "db_name": "workitems",
            "db_tier": "db-g1-small",  # GCP 등급 이름. Azure 로 넘어가면 안 된다
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


@pytest.mark.parametrize("config", [GCP, AZURE], ids=lambda config: config.name)
def test_every_variable_we_pass_is_declared_by_that_terraform_folder(config):
    declared = declared_variables(config.terraform_dir)

    assert config.known_variables <= declared, sorted(config.known_variables - declared)
    assert config.scope_variable in declared
    assert set(config.forced_variables) <= declared


def test_size_constants_describe_the_same_memory_values_the_terraform_accepts():
    # infra/azure 의 memory 검증 목록이 바뀌면(예: 상한을 늘림) 이 테스트가 실패해서 상수도 같이 고치게 한다.
    text = (AZURE.terraform_dir / "variables.tf").read_text(encoding="utf-8")
    match = re.search(r"contains\(\[([0-9,\s]+)\],\s*var\.memory\)", text)
    assert match, "infra/azure/variables.tf 에서 memory 검증 목록을 찾지 못했습니다"
    accepted = {int(number) for number in match.group(1).split(",") if number.strip()}

    steps = int(AZURE_MAX_MEMORY_MIB / AZURE_MEMORY_PER_VCPU_MIB / AZURE_CPU_STEP)
    ours = {int(step * AZURE_CPU_STEP * AZURE_MEMORY_PER_VCPU_MIB) for step in range(1, steps + 1)}

    assert ours == accepted, f"코드 상수가 만드는 메모리 {sorted(ours)} 와 Terraform 이 받는 {sorted(accepted)} 가 다릅니다"


def test_azure_config_values():
    assert AZURE.terraform_dir == get_provider("azure").terraform_dir
    assert (AZURE.terraform_dir / "main.tf").is_file()
    assert (AZURE.default_region, AZURE.scope_variable, AZURE.scope_env) == (
        "koreacentral", "subscription_id", "AZURE_SUBSCRIPTION_ID",
    )
    assert dict(AZURE.forced_variables) == {}  # create_vpn_key 는 GCP 전용


def test_gcp_only_variables_are_not_passed_to_azure():
    for name in ("db_tier", "db_user", "db_logical_decoding", "db_disk_size_gb", "project_id", "create_vpn_key"):
        assert name not in AZURE.known_variables


def test_azure_folder_can_be_overridden_by_its_own_variable(monkeypatch):
    monkeypatch.delenv("PROVISIONING_TF_DIR", raising=False)
    monkeypatch.setenv("PROVISIONING_TF_DIR_AZURE", "/custom/azure")

    assert terraform_dir(AZURE) == Path("/custom/azure")


def test_the_gcp_only_folder_variable_does_not_point_azure_at_the_gcp_folder(monkeypatch):
    monkeypatch.setenv("PROVISIONING_TF_DIR", "/legacy/gcp")
    monkeypatch.delenv("PROVISIONING_TF_DIR_AZURE", raising=False)

    assert terraform_dir(AZURE) == AZURE.terraform_dir


def test_missing_subscription_names_the_azure_variable(monkeypatch):
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)

    with pytest.raises(ProvisioningError, match="AZURE_SUBSCRIPTION_ID"):
        resolve_project_id(None, "azure")


# ---------------------------------------------------------------------------
# 앱 크기 번역 (vCPU 1개당 2048 MiB, 0.25 vCPU 단위, 최대 4096 MiB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpu", "memory", "expected"),
    [
        (1, 512, (1.0, 2048)),  # 조사 결과의 흔한 값. 그대로 두면 Azure 가 거부한다
        (0.5, 1024, (0.5, 1024)),  # 이미 유효한 조합은 그대로
        (2, 1024, (2.0, 4096)),
        (1, 3000, (1.5, 3072)),  # 메모리가 더 크면 vCPU 를 올려서 덮는다
        (None, 512, (0.25, 512)),
        (1, None, (1.0, 2048)),
        (0.1, 100, (0.25, 512)),  # 최소 조합
        (4, 8192, (2.0, 4096)),  # 한도를 넘으면 한도로 맞춘다
    ],
)
def test_app_size_is_translated_to_a_combination_azure_accepts(cpu, memory, expected):
    infra, _ = azure_adapt({"cpu": cpu, "memory": memory, "service_name": "x"})

    assert (infra["cpu"], infra["memory"]) == expected
    assert infra["memory"] % 512 == 0 and infra["memory"] <= 4096
    assert infra["memory"] == infra["cpu"] * 2048  # Container Apps 의 vCPU 당 메모리 규칙


def test_translation_explains_what_it_changed_and_what_it_capped():
    _, changed = azure_adapt({"cpu": 1, "memory": 512})
    _, same = azure_adapt({"cpu": 0.5, "memory": 1024})
    _, capped = azure_adapt({"cpu": 4, "memory": 8192})

    assert len(changed) == 1 and "cpu 1.0" in changed[0] and "memory 2048" in changed[0]
    assert same == []
    assert len(capped) == 1 and "한도" in capped[0]


def test_unknown_size_is_left_to_the_azure_defaults():
    infra, notes = azure_adapt({"service_name": "x"})

    assert "cpu" not in infra and "memory" not in infra and notes == []


def test_a_non_numeric_size_is_an_error():
    with pytest.raises(ProvisioningError, match="cpu"):
        azure_adapt({"cpu": "big", "memory": 512})


def test_translation_does_not_modify_the_callers_dictionary():
    original = {"cpu": 1, "memory": 512}
    azure_adapt(original)

    assert original == {"cpu": 1, "memory": 512}


# ---------------------------------------------------------------------------
# provisioning / app_deploy 단계 전체 (Azure)
# ---------------------------------------------------------------------------


def test_tfvars_use_azure_names_and_drop_gcp_values():
    values = build_tfvars(
        {"service_name": "target-app", "db_tier": "db-g1-small", "db_user": "app", "cpu": 1.0},
        project_id="sub-1", deploy_app=False, provider="azure",
    )

    assert values == {"service_name": "target-app", "cpu": 1.0, "subscription_id": "sub-1", "deploy_app": False}


def test_azure_provisioning_writes_translated_variables_and_builds_the_target():
    tf, ctx = FakeTerraform(outputs=azure_outputs()), FakeContext()
    result = run(azure_input(), ctx, terraform=tf)

    assert tf.var_values == {
        "region": "koreacentral",
        "service_name": "target-app",
        "db_name": "workitems",
        "cpu": 1.0,
        "memory": 2048,
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "deploy_app": False,
    }
    assert result.target["db"]["private_ip"] == "10.20.2.4"
    assert result.target["network"]["vpc_name"] == "vnet-target-app"
    assert result.target["project_id"] == "00000000-0000-0000-0000-000000000000"
    assert result.problems and all("target-cloud" in problem for problem in result.problems)  # Azure 에는 VPN 키가 없다
    assert any("앱 크기를 Azure 규칙에 맞췄습니다" in (message or "") for _, _, message in ctx.calls)


def test_azure_provisioning_stores_the_admin_secret_and_warns_about_the_missing_vpn_key():
    result = run(azure_input(), FakeContext(), terraform=FakeTerraform(outputs=azure_outputs()))

    refs = result.target["credentials_ref"]
    assert resolve(refs["admin"]) == {"user": "alphaadmin", "password": ADMIN_PASSWORD}
    with pytest.raises(KeyError):
        resolve(refs["cloud"])
    assert ADMIN_PASSWORD not in result.model_dump_json()


def test_azure_region_default_applies_when_the_plan_has_none():
    infra = {"service_name": "target-app"}
    result = run(azure_input(target_infra=infra), FakeContext(), terraform=FakeTerraform(outputs=azure_outputs()))

    assert result.target["region"] == "koreacentral"


def test_azure_deploy_app_turns_the_app_on_with_the_same_translated_size():
    outputs = azure_outputs()
    outputs["app_url"]["value"] = "https://target-app.example.azurecontainerapps.io"
    tf = FakeTerraform(outputs=outputs)
    result = deploy_app(
        AppDeployInput(
            run_id="mig-azure-001", provider="azure", project_id="sub-1", update_worker_target=False,
            target_infra={"service_name": "target-app", "cpu": 1, "memory": 512, "db_tier": "db-g1-small"},
        ),
        FakeContext(), terraform=tf, http_get=lambda url: 200, sleep=lambda s: None, clock=fake_clock(),
    )

    assert tf.var_values == {
        "service_name": "target-app", "cpu": 1.0, "memory": 2048, "subscription_id": "sub-1", "deploy_app": True,
    }
    assert result.healthy is True and result.app_url == "https://target-app.example.azurecontainerapps.io"


def test_azure_login_uses_the_fixed_user_and_the_az_token():
    from app.features.provisioning.image_transfer import ACR_TOKEN_USER, azure_login

    seen = []
    login = azure_login("acrtargetapp.azurecr.io", token_source=lambda registry: seen.append(registry) or "TOKEN")

    assert (login.registry, login.username, login.password) == ("acrtargetapp.azurecr.io", ACR_TOKEN_USER, "TOKEN")
    assert seen == ["acrtargetapp.azurecr.io"]
    assert destination_login.__name__  # 등록된 목적지 로그인 목록에도 있다
    from app.features.provisioning.image_transfer import DESTINATION_LOGINS

    assert set(DESTINATION_LOGINS) == {"gcp", "azure"}


def _fake_run(returncode=0, stdout="TOKEN\n", raises=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if raises:
            raise raises
        return type("Done", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()

    return run, calls


def test_the_az_token_command_gets_only_the_registry_name(monkeypatch):
    from app.features.provisioning import image_transfer

    run, calls = _fake_run()
    monkeypatch.setattr(image_transfer.subprocess, "run", run)

    assert image_transfer._az_acr_token("acrtargetapp.azurecr.io") == "TOKEN"
    assert calls[0][:5] == ["az", "acr", "login", "--name", "acrtargetapp"]
    assert "--expose-token" in calls[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"raises": FileNotFoundError()}, "az CLI"),
        ({"returncode": 1, "stdout": ""}, "AcrPush"),
        ({"stdout": "  \n"}, "AcrPush"),
    ],
)
def test_a_failed_az_token_is_a_clear_error_without_output(monkeypatch, kwargs, message):
    from app.features.provisioning import image_transfer

    run, _ = _fake_run(**kwargs)
    monkeypatch.setattr(image_transfer.subprocess, "run", run)

    with pytest.raises(ProvisioningError, match=message):
        image_transfer._az_acr_token("acrtargetapp.azurecr.io")
