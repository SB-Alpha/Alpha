from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.features.provisioning import service as provisioning_service
from app.features.workflows import runners, target_adapters
from app.features.workflows.engine import Engine
from app.features.workflows.router import get_engine
from app.features.workflows.schemas import MigrationRequest, RunStatus, TargetInfraConfig
from app.features.workflows.store import RunStore
from app.features.workflows.target_adapters import UnsupportedTarget, adapter_for, provider_of
from app.main import app

SIMPLE_BODY = {
    "service_name": "target-app",
    "aws_region": "ap-northeast-2",
    "aws_credentials_ref": "secret://aws/default",
    "db_admin_ref": "secret://db/admin",
}
TARGET_INFRA = {"region": "asia-northeast3", "service_name": "target-app"}


class RecordingContext:
    def report(self, state: str, progress: int, metrics: Any = None, message: str | None = None) -> None:
        pass


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = Engine(RunStore(tmp_path / "workflows.sqlite3"))
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _run_at_provisioning(tmp_path, request: MigrationRequest) -> RunStatus:
    run = Engine(RunStore(tmp_path / "runs.sqlite3")).create_run(request)
    return run.model_copy(update={"outputs": {"planning": {"plan": {"target_infra": TARGET_INFRA}}}})


# -- 간소화한 요청 ---------------------------------------------------------------


def test_simple_payload_needs_no_repository_and_derives_the_ecs_service():
    request = MigrationRequest(**SIMPLE_BODY)

    assert request.github_repo_url is None
    assert request.ecs_service == "target-app"
    # 비워 두면 assessment 가 계정의 RDS 가 하나뿐일 때 고릅니다. 이름 규칙으로 추측하지 않습니다.
    assert request.rds_instance_id is None
    assert request.target_region == "asia-northeast3"


def test_explicit_ecs_service_wins_over_the_derived_name():
    assert MigrationRequest(**SIMPLE_BODY, ecs_service="other-service").ecs_service == "other-service"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_service_name_is_treated_as_missing(blank):
    request = MigrationRequest(aws_region="ap-northeast-2", service_name=blank)
    assert (request.service_name, request.ecs_service) == (None, None)


def test_stored_requests_without_new_fields_still_load():
    stored = {"github_repo_url": "https://github.com/example/target-app", "aws_region": "ap-northeast-2"}
    request = MigrationRequest.model_validate(stored)
    assert (request.service_name, request.target_infra) == (None, None)
    assert provider_of(request) == "gcp"


def test_validation_is_stable_across_a_stored_round_trip():
    request = MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "gcp", "region": "asia-northeast1"})
    assert MigrationRequest.model_validate(request.model_dump()) == request


def test_create_run_accepts_the_simple_payload(client):
    created = client.post("/runs", json=SIMPLE_BODY)

    assert created.status_code == 201
    assert created.json()["request"]["ecs_service"] == "target-app"


def test_detail_reports_the_cdc_job_id(client):
    run_id = client.post("/runs", json=SIMPLE_BODY).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["cdc_job_id"] == run_id.replace("-", "_")


# -- 목적지 설정 ------------------------------------------------------------------


def test_target_infra_region_fills_the_target_region():
    request = MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "gcp", "region": "asia-northeast1"})
    assert request.target_region == "asia-northeast1"


def test_conflicting_target_regions_are_rejected():
    with pytest.raises(ValidationError, match="target_region"):
        MigrationRequest(
            **SIMPLE_BODY,
            target_region="asia-northeast3",
            target_infra={"provider": "gcp", "region": "asia-northeast1"},
        )


@pytest.mark.parametrize("key", ["password", "db_password", "api_key", "accessToken", "client_secret"])
def test_extra_params_reject_raw_secret_values(key):
    with pytest.raises(ValidationError, match="secret://"):
        TargetInfraConfig(provider="gcp", extra_params={key: "plain-text-value"})


def test_extra_params_accept_references_and_plain_settings():
    params = {"client_secret": "secret://azure/client", "sku": "Standard_B1ms"}
    assert TargetInfraConfig(provider="azure", extra_params=params).extra_params == params


# -- 어댑터 선택 ------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ncp"])  # azure 는 어댑터가 등록되어 거절 대상이 아니다
def test_create_run_refuses_a_target_without_an_adapter(client, provider):
    response = client.post("/runs", json={**SIMPLE_BODY, "target_infra": {"provider": provider}})

    assert response.status_code == 422
    assert provider in response.json()["detail"]
    assert client.get("/runs").json() == []


def test_unknown_provider_is_rejected_by_the_schema(client):
    assert client.post("/runs", json={**SIMPLE_BODY, "target_infra": {"provider": "aws"}}).status_code == 422


def test_adapter_defaults_to_gcp_for_requests_without_target_infra():
    assert adapter_for(MigrationRequest(**SIMPLE_BODY)).provider == "gcp"


def test_provisioning_refuses_instead_of_building_another_cloud(tmp_path, monkeypatch):
    def must_not_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("GCP 를 대신 구축하면 안 됩니다")

    monkeypatch.setattr(provisioning_service, "run", must_not_run)
    run = _run_at_provisioning(tmp_path, MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "ncp"}))

    with pytest.raises(UnsupportedTarget, match="ncp"):
        runners.run_provisioning(run, RecordingContext())


def test_gcp_provisioning_passes_the_requested_project(tmp_path, monkeypatch):
    seen: list[Any] = []
    monkeypatch.setattr(provisioning_service, "run", lambda inp, ctx: seen.append(inp) or {"target": {}})
    target = {"provider": "gcp", "project_id": "my-project"}
    run = _run_at_provisioning(tmp_path, MigrationRequest(**SIMPLE_BODY, target_infra=target))

    runners.run_provisioning(run, RecordingContext())

    assert seen[0].project_id == "my-project"
    assert seen[0].target_infra == TARGET_INFRA


def test_gcp_deploy_updates_the_worker_only_when_it_is_configured(tmp_path, monkeypatch):
    seen: list[Any] = []
    monkeypatch.setattr(provisioning_service, "deploy_app", lambda inp, ctx: seen.append(inp) or {"healthy": True})
    run = _run_at_provisioning(tmp_path, MigrationRequest(**SIMPLE_BODY))

    monkeypatch.delenv("WORKER_URL", raising=False)
    runners.run_app_deploy(run, RecordingContext())
    monkeypatch.setenv("WORKER_URL", "http://worker.test")
    runners.run_app_deploy(run, RecordingContext())

    assert [inp.update_worker_target for inp in seen] == [False, True]


def _run_with_source(tmp_path, source) -> RunStatus:
    run = _run_at_provisioning(tmp_path, MigrationRequest(**SIMPLE_BODY))
    outputs = {"planning": {"plan": {"target_infra": TARGET_INFRA}, "source": source}}
    return run.model_copy(update={"outputs": outputs})


def test_the_source_key_reference_from_the_assessment_reaches_the_app_deploy_input(tmp_path, monkeypatch):
    seen: list[Any] = []
    monkeypatch.setattr(provisioning_service, "deploy_app", lambda inp, ctx: seen.append(inp) or {"healthy": True})
    run = _run_with_source(tmp_path, {"credentials_ref": {"cloud": "secret://aws/source", "db_admin": "secret://db/a"}})

    runners.run_app_deploy(run, RecordingContext())

    assert seen[0].source_credentials_ref == "secret://aws/source"


def test_the_source_key_reference_is_read_from_pydantic_models_too(tmp_path):
    from app.features.assessment.schemas import SourceCredentials, SourceInfo

    source = SourceInfo(credentials_ref=SourceCredentials(cloud="secret://aws/source"))

    assert runners._extract_source_credentials_ref(_run_with_source(tmp_path, source)) == "secret://aws/source"


@pytest.mark.parametrize(
    "source",
    [None, {}, {"credentials_ref": None}, {"credentials_ref": {}}, {"credentials_ref": {"cloud": None}}, {"credentials_ref": {"cloud": ""}}],
)
def test_a_missing_source_key_reference_is_none_not_an_error(tmp_path, monkeypatch, source):
    seen: list[Any] = []
    monkeypatch.setattr(provisioning_service, "deploy_app", lambda inp, ctx: seen.append(inp) or {"healthy": True})
    run = _run_with_source(tmp_path, source)

    runners.run_app_deploy(run, RecordingContext())  # 조사 출력에 키 이름표가 없어도 앱 배포는 이전처럼 진행된다

    assert seen[0].source_credentials_ref is None


def test_an_adapter_receives_the_source_key_reference_as_an_argument(tmp_path, monkeypatch):
    class FakeAzure:
        provider = "azure"

        def deploy_app(self, run_id, target_infra, config, ctx, source_credentials_ref=None):
            return {"source_credentials_ref": source_credentials_ref}

    monkeypatch.setattr(target_adapters, "TARGET_ADAPTERS", {"azure": FakeAzure()})
    request = MigrationRequest(**SIMPLE_BODY, target_infra={"provider": "azure"})
    run = _run_with_source(tmp_path, {"credentials_ref": {"cloud": "secret://aws/source"}})
    run = run.model_copy(update={"request": request})

    assert runners.run_app_deploy(run, RecordingContext()) == {"source_credentials_ref": "secret://aws/source"}


def test_registering_an_adapter_routes_provisioning_without_runner_changes(tmp_path, monkeypatch):
    class FakeAzure:
        provider = "azure"

        def provision(self, run_id, target_infra, config, ctx):
            return {"run_id": run_id, "extra": config.extra_params}

        def deploy_app(self, run_id, target_infra, config, ctx, source_credentials_ref=None):
            return {"healthy": True, "source_credentials_ref": source_credentials_ref}

    monkeypatch.setattr(target_adapters, "TARGET_ADAPTERS", {"azure": FakeAzure()})
    target = {"provider": "azure", "extra_params": {"sku": "Standard_B1ms"}}
    run = _run_at_provisioning(tmp_path, MigrationRequest(**SIMPLE_BODY, target_infra=target))

    result = runners.run_provisioning(run, RecordingContext())

    assert result == {"run_id": run.run_id, "extra": {"sku": "Standard_B1ms"}}


def test_target_infra_extra_params_rejects_nested_raw_secrets():
    with pytest.raises(ValidationError) as exc:
        TargetInfraConfig(provider="gcp", extra_params={"azure": {"client_secret": "plain-text-secret"}})
    assert "extra_params.azure.client_secret" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        TargetInfraConfig(provider="gcp", extra_params={"items": [{"db_password": "plain"}]})
    assert "extra_params.items[0].db_password" in str(exc.value)

    # secret:// reference passes
    cfg = TargetInfraConfig(
        provider="gcp",
        extra_params={"azure": {"client_secret": "secret://azure/cred"}, "items": [{"token": "secret://tok"}]},
    )
    assert cfg.extra_params["azure"]["client_secret"] == "secret://azure/cred"
