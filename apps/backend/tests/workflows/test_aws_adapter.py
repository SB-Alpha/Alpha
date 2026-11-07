from typing import Any

import pytest

from app.features.provisioning import service as provisioning_service
from app.features.workflows import target_adapters
from app.features.workflows.schemas import MigrationRequest, TargetInfraConfig
from app.features.workflows.target_adapters import AwsTargetAdapter, adapter_for

from .test_target_adapters import SIMPLE_BODY

ACCOUNT = "123456789012"


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


def test_aws_is_registered():
    assert isinstance(target_adapters.TARGET_ADAPTERS["aws"], AwsTargetAdapter)


def test_adapter_for_picks_aws_when_the_request_asks_for_it():
    request = MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "aws"})

    assert adapter_for(request).provider == "aws"


def test_provision_hands_provisioning_the_aws_provider_and_the_account(seen):
    config = TargetInfraConfig(provider="aws", project_id=ACCOUNT, region="ap-northeast-1")
    AwsTargetAdapter().provision("mig-1", {"service_name": "target-app", "region": "asia-northeast3"}, config, Ctx())

    inp = seen["run"][0]
    assert (inp.provider, inp.project_id) == ("aws", ACCOUNT)
    assert inp.target_infra["region"] == "ap-northeast-1"  # 요청의 리전이 조사 결과보다 우선한다


def test_provision_without_a_config_leaves_the_account_to_the_environment(seen):
    AwsTargetAdapter().provision("mig-1", {"service_name": "target-app"}, None, Ctx())

    assert seen["run"][0].project_id is None
    assert seen["run"][0].provider == "aws"


def test_the_plan_dictionary_is_not_modified(seen):
    plan = {"service_name": "target-app", "region": "asia-northeast3"}
    AwsTargetAdapter().provision("mig-1", plan, TargetInfraConfig(provider="aws", region="ap-northeast-2"), Ctx())

    assert plan == {"service_name": "target-app", "region": "asia-northeast3"}


def test_deploy_app_passes_the_source_key_reference_on(seen, monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    AwsTargetAdapter().deploy_app("mig-1", {}, None, Ctx(), source_credentials_ref="secret://mig-1/source-cloud")

    inp = seen["deploy_app"][0]
    assert (inp.provider, inp.source_credentials_ref, inp.update_worker_target) == ("aws", "secret://mig-1/source-cloud", False)


def test_deploy_app_tells_the_worker_only_when_worker_url_is_set(seen, monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://worker.example")
    AwsTargetAdapter().deploy_app("mig-1", {}, None, Ctx())

    assert seen["deploy_app"][0].update_worker_target is True


class RecordingCtx:
    def __init__(self):
        self.messages: list[str] = []

    def report(self, state, progress, metrics=None, message=None):
        self.messages.append(message or "")


def test_a_gcp_region_from_the_request_default_is_dropped_so_the_aws_default_applies(seen):
    # 요청에 리전이 없으면 target_region 기본값(asia-northeast3)이 조사 결과로 흘러온다
    ctx = RecordingCtx()
    AwsTargetAdapter().provision("mig-1", {"service_name": "target-app", "region": "asia-northeast3"}, None, ctx)

    assert "region" not in seen["run"][0].target_infra
    assert any("asia-northeast3" in message and "ap-northeast-2" in message for message in ctx.messages)


@pytest.mark.parametrize("region", ["ap-northeast-2", "us-east-1", "eu-central-1", "us-gov-west-1"])
def test_an_aws_region_is_kept(seen, region):
    ctx = RecordingCtx()
    AwsTargetAdapter().provision("mig-1", {"service_name": "target-app", "region": region}, None, ctx)

    assert seen["run"][0].target_infra["region"] == region
    assert ctx.messages == []


def test_an_explicit_gcp_region_in_the_config_is_also_dropped(seen):
    config = TargetInfraConfig(provider="aws", region="koreacentral")
    AwsTargetAdapter().deploy_app("mig-1", {"service_name": "target-app"}, config, RecordingCtx())

    assert "region" not in seen["deploy_app"][0].target_infra
