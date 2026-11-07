import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.features.provisioning import providers
from app.features.provisioning.providers import GCP, PROVIDERS, ProviderConfig, get_provider, terraform_dir
from app.features.provisioning.schemas import AppDeployInput, ProviderName, ProvisioningInput
from app.features.provisioning.service import build_tfvars, deploy_app, resolve_project_id, run
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FIXTURE, FakeContext, FakeTerraform, fake_clock, with_app_url


def fake_provider_input(model, **fields):
    # "other" 는 시험용 가짜 목적지라 입력 검증(등록된 이름만 받음)을 거치지 않고 만든다.
    # 단계 코드가 이름으로 설정을 고르는 부분만 확인하는 시험에서만 쓴다.
    return model.model_construct(**fields)

# 목적지가 하나 더 있을 때를 흉내 낸 설정. 변수 이름·환경변수·강제 변수가 GCP 와 모두 다르다.
OTHER = ProviderConfig(
    name="other",
    terraform_dir=Path("/opt/terraform/other"),
    terraform_dir_env=("PROVISIONING_TF_DIR_OTHER",),
    known_variables=frozenset({"region", "service_name", "cpu"}),
    default_region="otherland-1",
    scope_variable="subscription_id",
    scope_env="OTHER_SUBSCRIPTION_ID",
    scope_label="Other 구독 ID",
)


@pytest.fixture
def other_provider(monkeypatch):
    monkeypatch.setitem(PROVIDERS, OTHER.name, OTHER)
    for name in ("PROVISIONING_TF_DIR", "PROVISIONING_TF_DIR_GCP", "PROVISIONING_TF_DIR_OTHER", "OTHER_SUBSCRIPTION_ID"):
        monkeypatch.delenv(name, raising=False)
    return OTHER


# ---------------------------------------------------------------------------
# GCP 설정은 service.py 에 하드코딩되어 있던 값과 같다
# ---------------------------------------------------------------------------


def test_gcp_config_keeps_the_values_that_used_to_be_hardcoded():
    assert GCP.default_region == "asia-northeast3"
    assert GCP.scope_variable == "project_id"
    assert GCP.scope_env == "GCP_PROJECT_ID"
    assert dict(GCP.forced_variables) == {"create_vpn_key": True}
    assert GCP.terraform_dir == providers.REPO_ROOT / "infra" / "terraform" / "gcp"
    assert GCP.known_variables == {
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


def test_the_default_provider_is_gcp_so_existing_callers_need_no_change():
    assert ProvisioningInput(run_id="r", target_infra={}).provider == "gcp"
    assert AppDeployInput(run_id="r", target_infra={}).provider == "gcp"


def test_the_gcp_terraform_folder_exists():
    assert (GCP.terraform_dir / "main.tf").is_file()


# ---------------------------------------------------------------------------
# 목적지 고르기
# ---------------------------------------------------------------------------


def test_unknown_provider_is_a_clear_error_that_lists_the_registered_ones():
    with pytest.raises(ProvisioningError, match=r"'nope'.*gcp"):
        get_provider("nope")


def test_the_input_accepts_exactly_the_registered_provider_names():
    # schemas.py 는 구현을 import 하지 않으려고 이름을 Literal 로 따로 가진다. 등록표와 어긋나면 여기서 걸린다.
    assert set(get_args(ProviderName)) == set(PROVIDERS)


@pytest.mark.parametrize("model", [ProvisioningInput, AppDeployInput])
def test_a_misspelled_provider_is_rejected_by_input_validation(model):
    with pytest.raises(ValidationError, match="provider"):
        model(run_id="r", provider="gpc", project_id="p", target_infra={})


def test_an_unregistered_provider_still_stops_before_terraform_is_touched():
    # 입력 검증을 건너뛰어 만든 값이 service 까지 와도 Terraform 을 건드리기 전에 멈춘다(두 번째 방어선).
    tf = FakeTerraform()
    inp = fake_provider_input(ProvisioningInput, run_id="r", provider="nope", project_id="p", target_infra={})
    with pytest.raises(ProvisioningError, match="'nope'"):
        run(inp, FakeContext(), terraform=tf)
    assert tf.calls == []


# ---------------------------------------------------------------------------
# Terraform 폴더
# ---------------------------------------------------------------------------


def test_terraform_folder_honors_the_legacy_variable_for_gcp_only(monkeypatch, other_provider):
    monkeypatch.setenv("PROVISIONING_TF_DIR", "/legacy/tf")

    assert terraform_dir(GCP) == Path("/legacy/tf")
    assert terraform_dir(other_provider) == other_provider.terraform_dir  # 다른 목적지는 옛 이름을 읽지 않는다


def test_a_provider_specific_variable_wins_over_the_legacy_one(monkeypatch, other_provider):
    monkeypatch.setenv("PROVISIONING_TF_DIR", "/legacy/tf")
    monkeypatch.setenv("PROVISIONING_TF_DIR_GCP", "/gcp/tf")
    monkeypatch.setenv("PROVISIONING_TF_DIR_OTHER", "/other/tf")

    assert terraform_dir(GCP) == Path("/gcp/tf")
    assert terraform_dir(other_provider) == Path("/other/tf")


# ---------------------------------------------------------------------------
# 변수 이름·소속 단위·강제 변수
# ---------------------------------------------------------------------------


def test_tfvars_use_the_providers_own_names_and_forced_variables(other_provider):
    values = build_tfvars(
        {"cpu": 2, "db_user": "x", "memory": 512}, project_id="sub-1", deploy_app=False, provider="other"
    )

    # db_user, memory 는 이 목적지가 선언하지 않은 변수라 빠지고, GCP 전용 create_vpn_key 도 붙지 않는다.
    assert values == {"cpu": 2, "subscription_id": "sub-1", "deploy_app": False}


def test_missing_scope_names_the_providers_variable(other_provider):
    with pytest.raises(ProvisioningError) as error:
        resolve_project_id(None, "other")

    assert "Other 구독 ID" in str(error.value)
    assert "OTHER_SUBSCRIPTION_ID" in str(error.value)
    assert "GCP" not in str(error.value)


def test_scope_falls_back_to_the_providers_environment_variable(monkeypatch, other_provider):
    monkeypatch.setenv("OTHER_SUBSCRIPTION_ID", "from-env")

    assert resolve_project_id(None, "other") == "from-env"
    assert resolve_project_id("given", "other") == "given"


# ---------------------------------------------------------------------------
# provisioning / app_deploy 단계 전체
# ---------------------------------------------------------------------------


def test_run_with_another_provider_writes_its_variables_and_uses_its_default_region(other_provider):
    tf = FakeTerraform()
    result = run(
        fake_provider_input(
            ProvisioningInput,
            run_id="mig-001",
            provider="other",
            project_id="sub-1",
            target_infra={"service_name": "target-app", "db_user": "dropped"},
        ),
        FakeContext(),
        terraform=tf,
    )

    assert tf.var_values == {"service_name": "target-app", "subscription_id": "sub-1", "deploy_app": False}
    assert result.target["region"] == "otherland-1"  # target_infra 에 region 이 없으면 이 목적지의 기본값
    assert result.target["project_id"] == "sub-1"


def test_an_explicit_region_beats_the_providers_default(other_provider):
    result = run(
        fake_provider_input(
            ProvisioningInput, run_id="r", provider="other", project_id="sub-1", target_infra={"region": "elsewhere-2"}
        ),
        FakeContext(),
        terraform=FakeTerraform(),
    )

    assert result.target["region"] == "elsewhere-2"


def test_deploy_app_with_another_provider_turns_the_app_on_with_its_variables(other_provider):
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8")), "https://target-app.example.net"))
    result = deploy_app(
        fake_provider_input(
            AppDeployInput,
            run_id="mig-001",
            provider="other",
            project_id="sub-1",
            target_infra={"service_name": "target-app"},
            update_worker_target=False,
        ),
        FakeContext(),
        terraform=tf,
        http_get=lambda url: 200,
        sleep=lambda s: None,
        clock=fake_clock(),
    )

    assert tf.var_values == {"service_name": "target-app", "subscription_id": "sub-1", "deploy_app": True}
    assert result.app_url == "https://target-app.example.net"
    assert result.healthy is True
