"""AWS 목적지 계정이 원본 계정과 같으면 목적지를 만들기 전에 거부한다."""

import pytest

from app.features.provisioning import service as provisioning_service
from app.features.workflows import runners
from app.features.workflows.engine import Engine
from app.features.workflows.schemas import MigrationRequest
from app.features.workflows.store import RunStore

SOURCE = "061292478449"
OTHER = "123456789012"
BODY = {"aws_region": "ap-northeast-2", "aws_credentials_ref": "secret://source-cloud"}


def make_run(tmp_path, *, provider="aws", project_id=None, source_account=SOURCE):
    infra = {"provider": provider}
    if project_id:
        infra["project_id"] = project_id
    request = MigrationRequest(github_repo_url="https://github.com/example/app", target_infra=infra, **BODY)
    created = Engine(RunStore(tmp_path / "runs.sqlite3"), {}).create_run(request)
    outputs = {"plan": {"target_infra": {"region": "ap-northeast-2"}}}
    planning = dict(outputs)
    if source_account is not None:
        planning["source"] = {"account_id": source_account}
    return created.model_copy(update={"outputs": {"planning": planning}})


@pytest.fixture
def built(monkeypatch):
    calls = []
    monkeypatch.setattr(provisioning_service, "run", lambda inp, ctx: calls.append(inp) or {"target": {}})
    return calls


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv(runners.ALLOW_SAME_ACCOUNT_ENV, raising=False)


def test_the_same_account_in_the_request_is_refused_before_anything_is_built(tmp_path, built):
    run = make_run(tmp_path, project_id=SOURCE)

    with pytest.raises(RuntimeError, match=SOURCE):
        runners.run_provisioning(run, ctx=None)
    assert built == []


def test_the_same_account_from_the_environment_variable_is_refused_too(tmp_path, built, monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", SOURCE)

    with pytest.raises(RuntimeError, match="원본 계정과 같습니다"):
        runners.run_provisioning(make_run(tmp_path), ctx=None)
    assert built == []


def test_a_different_account_goes_through(tmp_path, built):
    runners.run_provisioning(make_run(tmp_path, project_id=OTHER), ctx=None)

    assert len(built) == 1


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_the_same_account_is_allowed_when_it_is_stated_explicitly(tmp_path, built, monkeypatch, value):
    monkeypatch.setenv(runners.ALLOW_SAME_ACCOUNT_ENV, value)

    runners.run_provisioning(make_run(tmp_path, project_id=SOURCE), ctx=None)

    assert len(built) == 1


@pytest.mark.parametrize("value", ["", "0", "no", "maybe"])
def test_other_values_do_not_count_as_allowing_it(tmp_path, built, monkeypatch, value):
    monkeypatch.setenv(runners.ALLOW_SAME_ACCOUNT_ENV, value)

    with pytest.raises(RuntimeError):
        runners.run_provisioning(make_run(tmp_path, project_id=SOURCE), ctx=None)


def test_nothing_is_compared_when_the_source_account_is_unknown(tmp_path, built):
    # 원본을 직접 입력한 경우처럼 계정 ID 가 없으면 비교할 수 없다
    runners.run_provisioning(make_run(tmp_path, project_id=SOURCE, source_account=None), ctx=None)

    assert len(built) == 1


def test_other_destinations_are_not_checked(tmp_path, built):
    runners.run_provisioning(make_run(tmp_path, provider="gcp", project_id=SOURCE), ctx=None)

    assert len(built) == 1


def test_surrounding_spaces_in_the_request_value_do_not_hide_the_match(tmp_path, built):
    with pytest.raises(RuntimeError):
        runners.run_provisioning(make_run(tmp_path, project_id=f" {SOURCE} "), ctx=None)
