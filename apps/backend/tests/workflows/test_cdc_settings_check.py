"""Settings the CDC stage needs are checked before the target is built."""

import pytest

from app.features.provisioning import service as provisioning_service
from app.features.workflows import runners
from app.features.workflows.engine import Engine
from app.features.workflows.schemas import MigrationRequest
from app.features.workflows.store import RunStore

OCI_ENV = {
    "CDC_OCI_REGION": "oci-1", "CDC_OCI_COMPARTMENT_ID": "c", "CDC_OCI_DRG_ID": "d",
    "CDC_TFSTATE_BUCKET": "b", "CDC_TFSTATE_NAMESPACE": "n",
    "CDC_OCI_VCN_CIDR": "10.250.0.0/16", "CDC_NODE_SUBNET_CIDR": "10.250.1.0/24",
}
REQUEST = MigrationRequest(
    github_repo_url="https://github.com/example/app",
    aws_region="ap-northeast-2",
    aws_credentials_ref="secret://source-cloud",
)


@pytest.fixture
def built(monkeypatch):
    calls = []
    monkeypatch.setattr(provisioning_service, "run", lambda inp, ctx: calls.append(inp) or {"target": {}})
    return calls


@pytest.fixture
def run(tmp_path):
    created = Engine(RunStore(tmp_path / "runs.sqlite3"), {}).create_run(REQUEST)
    plan = {"target_infra": {"region": "asia-east1"}}
    return created.model_copy(update={"outputs": {"planning": {"plan": plan}}})


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)
    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    for name, value in OCI_ENV.items():
        monkeypatch.setenv(name, value)


def test_target_is_built_when_the_settings_are_there(run, built):
    runners.run_provisioning(run, ctx=None)
    assert len(built) == 1


@pytest.mark.parametrize("name", sorted(OCI_ENV))
def test_missing_oci_setting_is_named_before_the_target_is_built(run, built, monkeypatch, name):
    monkeypatch.delenv(name)

    with pytest.raises(RuntimeError, match=name):
        runners.run_provisioning(run, ctx=None)
    assert built == []


def test_missing_agent_url_is_named_before_the_target_is_built(run, built, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL")

    with pytest.raises(RuntimeError, match="CDC_AGENT_URL"):
        runners.run_provisioning(run, ctx=None)
    assert built == []


def test_simulation_needs_no_oci_settings(run, built, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL")
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    for name in OCI_ENV:
        monkeypatch.delenv(name)

    runners.run_provisioning(run, ctx=None)
    assert len(built) == 1
