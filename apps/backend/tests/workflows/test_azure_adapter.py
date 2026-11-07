from typing import Any

import pytest

from app.features.provisioning import service as provisioning_service
from app.features.workflows import target_adapters
from app.features.workflows.schemas import MigrationRequest, TargetInfraConfig
from app.features.workflows.target_adapters import AzureTargetAdapter, GcpTargetAdapter, adapter_for

from .test_target_adapters import SIMPLE_BODY, client  # noqa: F401  (client 는 pytest 가 이름으로 찾는 fixture)

SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"


class Ctx:
    def report(self, state, progress, metrics=None, message=None):
        pass


@pytest.fixture
def seen(monkeypatch) -> dict[str, list[Any]]:
    """provisioning 을 실제로 부르지 않고 어떤 입력으로 불렸는지만 기록한다."""
    calls: dict[str, list[Any]] = {"run": [], "deploy_app": []}
    monkeypatch.setattr(provisioning_service, "run", lambda inp, ctx: calls["run"].append(inp) or {"target": {}})
    monkeypatch.setattr(provisioning_service, "deploy_app", lambda inp, ctx: calls["deploy_app"].append(inp) or {})
    return calls


# -- 등록 -----------------------------------------------------------------------


def test_azure_is_registered_next_to_gcp():
    assert isinstance(target_adapters.TARGET_ADAPTERS["azure"], AzureTargetAdapter)
    assert isinstance(target_adapters.TARGET_ADAPTERS["gcp"], GcpTargetAdapter)


def test_adapter_for_picks_azure_when_the_request_asks_for_it():
    request = MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "azure"})

    assert adapter_for(request).provider == "azure"


def test_create_run_accepts_an_azure_target_now_that_it_has_an_adapter(client):  # noqa: F811
    response = client.post("/runs", json={**SIMPLE_BODY, "target_infra": {"provider": "azure"}})

    assert response.status_code == 201


def test_unregistered_targets_are_still_refused(client):  # noqa: F811
    assert client.post("/runs", json={**SIMPLE_BODY, "target_infra": {"provider": "ncp"}}).status_code == 422


# -- provision ------------------------------------------------------------------


def test_provision_hands_provisioning_the_azure_provider_and_the_subscription(seen):
    config = TargetInfraConfig(provider="azure", project_id=SUBSCRIPTION, region="koreacentral")
    AzureTargetAdapter().provision("mig-1", {"service_name": "target-app", "region": "asia-northeast3"}, config, Ctx())

    inp = seen["run"][0]
    assert (inp.run_id, inp.provider, inp.project_id) == ("mig-1", "azure", SUBSCRIPTION)
    assert inp.target_infra["region"] == "koreacentral"  # 요청의 리전이 조사 결과의 GCP 리전보다 우선한다
    assert inp.target_infra["service_name"] == "target-app"


def test_resource_group_comes_from_extra_params(seen):
    config = TargetInfraConfig(provider="azure", extra_params={"resource_group": "rg-demo"})
    AzureTargetAdapter().provision("mig-1", {"service_name": "target-app"}, config, Ctx())

    assert seen["run"][0].target_infra["resource_group_name"] == "rg-demo"


def test_resource_group_name_is_accepted_as_an_alias(seen):
    config = TargetInfraConfig(provider="azure", extra_params={"resource_group_name": "rg-alias"})
    AzureTargetAdapter().provision("mig-1", {}, config, Ctx())

    assert seen["run"][0].target_infra["resource_group_name"] == "rg-alias"


def test_without_a_config_the_plan_values_pass_through_and_the_subscription_comes_from_the_environment(seen):
    AzureTargetAdapter().provision("mig-1", {"service_name": "target-app", "region": "koreacentral"}, None, Ctx())

    inp = seen["run"][0]
    assert inp.project_id is None  # 구독 ID 는 provisioning 이 AZURE_SUBSCRIPTION_ID 에서 읽는다
    assert inp.target_infra == {"service_name": "target-app", "region": "koreacentral"}
    assert "resource_group_name" not in inp.target_infra


def test_the_plan_dictionary_is_not_modified(seen):
    plan = {"service_name": "target-app"}
    AzureTargetAdapter().provision("mig-1", plan, TargetInfraConfig(provider="azure", region="koreacentral"), Ctx())

    assert plan == {"service_name": "target-app"}


# -- deploy_app -----------------------------------------------------------------


def test_deploy_app_uses_the_same_azure_values(seen, monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    config = TargetInfraConfig(provider="azure", project_id=SUBSCRIPTION, extra_params={"resource_group": "rg-demo"})
    AzureTargetAdapter().deploy_app("mig-1", {"service_name": "target-app"}, config, Ctx())

    inp = seen["deploy_app"][0]
    assert (inp.provider, inp.project_id) == ("azure", SUBSCRIPTION)
    assert inp.target_infra["resource_group_name"] == "rg-demo"
    assert inp.update_worker_target is False


def test_deploy_app_tells_the_worker_only_when_worker_url_is_set(seen, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://worker.example")
    AzureTargetAdapter().deploy_app("mig-1", {}, None, Ctx())

    assert seen["deploy_app"][0].update_worker_target is True


def test_deploy_app_passes_the_source_key_reference_on(seen, monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    AzureTargetAdapter().deploy_app("mig-1", {}, None, Ctx(), source_credentials_ref="secret://mig-1/source-cloud")

    assert seen["deploy_app"][0].source_credentials_ref == "secret://mig-1/source-cloud"


def test_deploy_app_without_a_source_key_reference_still_works(seen, monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    AzureTargetAdapter().deploy_app("mig-1", {}, None, Ctx())

    assert seen["deploy_app"][0].source_credentials_ref is None
