import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.features.db_migration.schemas import DbMigrationInput, TargetInput
from app.features.provisioning.onprem import service
from app.features.provisioning.onprem.models import OnpremConfig, OnpremTarget
from app.features.provisioning.onprem.state import State
from app.features.provisioning.schemas import AppDeployOutput, ProvisioningOutput
from app.features.provisioning.terraform import ProvisioningError


@pytest.fixture
def setup(tmp_path):
    cfg = dict(
        deployment_id="test-onprem",
        directory=str(tmp_path / "deploy"),
        docker_socket="/tmp/docker.sock",
        database="appdb",
        admin_ref="secret://admin",
        app_ref="secret://app",
        cloudflare_ref="secret://cf",
        worker_secret_ref="secret://worker",
        account_id="a" * 32,
        zone_id="b" * 32,
        tunnel_id="1" * 36,
        db_access_app_id="2" * 36,
        db_hostname="db.example.com",
        app_hostname="app.example.com",
        platform="linux/amd64",
    )
    secrets = {
        "secret://config": {"json": json.dumps(cfg)},
        "secret://admin": {"user": "admin", "password": "admin-sentinel"},
        "secret://app": {"user": "app", "password": "app-sentinel"},
        "secret://cf": {"api_token": "cf-sentinel", "tunnel_token": "tunnel-sentinel"},
        "secret://worker": {"value": "worker-sentinel"},
    }
    return cfg, secrets


class Runtime:
    def __init__(self):
        self.calls = []
        self.fail_internal = self.fail_external = self.fail_cdc = False

    def provision(self, *args):
        self.calls.append("provision")

    def probe_cdc(self):
        self.calls.append("probe")
        if self.fail_cdc:
            raise ProvisioningError("probe failed")

    def image(self, source, ref):
        self.calls.append("image")
        return {
            "image": "loaded:123",
            "source": source,
            "image_id": "sha256:" + "a" * 64,
            "archive_sha256": "b" * 64,
        }

    def deploy(self, admin, env, image):
        self.calls.append("deploy")
        self.env = env

    def internal_health(self, secret):
        self.calls.append("internal")
        if self.fail_internal:
            raise ProvisioningError("auth failed")

    def external_health(self, secret):
        self.calls.append("external")
        if self.fail_external:
            raise ProvisioningError("tunnel failed")

    def stop_was(self):
        self.calls.append("stop_was")

    def compose(self, args):
        self.calls.append("restore")


class Edge:
    def __init__(self):
        self.calls = []

    def check_db_access(self):
        self.calls.append("access")

    def ensure_route(self, host, service):
        self.calls.append((host, service))

    def healthy(self):
        self.calls.append("healthy")


class Worker:
    def __init__(self):
        self.targets = []
        self.mode = "SOURCE"

    def status(self):
        return {"mode": self.mode, "next_origin": self.mode}

    def set_target(self, url):
        self.targets.append(url)


def provision(setup, runtime=None, edge=None):
    return service.provision(
        run_id="run-1",
        config_ref="secret://config",
        resolver=setup[1].__getitem__,
        runtime=runtime or Runtime(),
        edge=edge or Edge(),
    )


def deploy(setup, runtime=None, edge=None, worker=None, **infra):
    return service.deploy_app(
        run_id="run-1",
        config_ref="secret://config",
        resolver=setup[1].__getitem__,
        target_infra={
            "source_image": "ecr.example/app@sha256:" + "a" * 64,
            "container_port": 8080,
            **infra,
        },
        source_credentials_ref="secret://aws",
        runtime=runtime or Runtime(),
        edge=edge or Edge(),
        worker=worker or Worker(),
    )


SOURCE = {
    "cloud": "aws",
    "account_id": "123456789012",
    "region": "ap-northeast-2",
    "db": {"host": "source.internal", "database": "postgres"},
    "network": {
        "vpc_id": "vpc-test",
        "cidr": "172.31.0.0/16",
        "route_table_ids": ["rtb-test"],
    },
    "app": {"db_user": "app"},
    "tables": ["public.work_items"],
    "credentials_ref": {"cloud": "secret://aws", "db_admin": "secret://source-admin"},
}


def test_fixed_output_accepted_by_existing_migration(setup):
    raw = provision(setup).model_dump(mode="json")
    assert set(raw) == set(ProvisioningOutput.model_fields)
    assert set(raw["target"]) == set(TargetInput.model_fields)
    mig = DbMigrationInput.model_validate(
        {
            "run_id": "run-1",
            "source": SOURCE,
            "target": raw["target"],
            "options": {"pass_onprem": True},
        }
    )
    assert (mig.target.db.private_ip, mig.target.db.port) == ("127.0.0.1", 15432)
    assert mig.target.app.url is None
    with pytest.raises(ValidationError):
        DbMigrationInput.model_validate(
            {"run_id": "run-1", "source": SOURCE, "target": raw["target"]}
        )


def test_deploy_preserves_target_except_url_and_no_secrets_in_output_or_state(setup):
    before = provision(setup).target.model_dump(mode="json")
    runtime, worker = Runtime(), Worker()
    out = deploy(
        setup,
        runtime,
        worker=worker,
        env_vars={"LOG_LEVEL": "INFO", "PGHOST": "old-db"},
    )
    assert set(out.model_dump()) == set(AppDeployOutput.model_fields)
    before["app"]["url"] = out.app_url
    assert out.target.model_dump(mode="json") == before
    assert (
        runtime.env["PGHOST"] == "postgres"
        and runtime.env["PGPASSWORD"] == "app-sentinel"
    )
    assert worker.targets == ["https://app.example.com"]
    assert out.ingress_guard == "SECURE"
    text = (
        out.model_dump_json() + (Path(setup[0]["directory"]) / "state.json").read_text()
    )
    for v in [
        "admin-sentinel",
        "app-sentinel",
        "cf-sentinel",
        "tunnel-sentinel",
        "worker-sentinel",
    ]:
        assert v not in text


def test_target_forbids_runtime_fields(setup):
    with pytest.raises(ValidationError):
        OnpremTarget.model_validate(
            {**provision(setup).target.model_dump(), "docker_socket": "/tmp/sock"}
        )


def test_cdc_failure_is_not_success(setup):
    r = Runtime()
    r.fail_cdc = True
    with pytest.raises(ProvisioningError):
        provision(setup, r)
    assert not (Path(setup[0]["directory"]) / "state.json").exists()


def test_requires_provision_and_provision_is_idempotent(setup):
    with pytest.raises(ProvisioningError, match="먼저"):
        deploy(setup)
    a = provision(setup)
    b = provision(setup)
    assert a.target == b.target and not b.applied


@pytest.mark.parametrize("failure", ["fail_internal", "fail_external"])
def test_failed_new_deploy_stops_only_was_without_worker_update(setup, failure):
    provision(setup)
    r, e, w = Runtime(), Edge(), Worker()
    setattr(r, failure, True)
    with pytest.raises(ProvisioningError, match="검증 실패"):
        deploy(setup, r, e, w)
    assert r.calls[-1] == "stop_was" and not w.targets
    if failure == "fail_internal":
        assert not e.calls
    assert State(OnpremConfig(**setup[0])).load()["deployment"] is None


def test_failed_redeploy_restores_previous(setup):
    provision(setup)
    deploy(setup)
    r = Runtime()
    r.fail_internal = True
    with pytest.raises(ProvisioningError):
        deploy(setup, r)
    assert r.calls[-1] == "restore"


def test_worker_target_blocks_deploy(setup):
    provision(setup)
    r, w = Runtime(), Worker()
    w.mode = "TARGET"
    with pytest.raises(ProvisioningError, match="SOURCE"):
        deploy(setup, r, worker=w)
    assert not r.calls


def test_config_change_refused(setup):
    provision(setup)
    setup[0]["database"] = "otherdb"
    setup[1]["secret://config"]["json"] = json.dumps(setup[0])
    with pytest.raises(ProvisioningError, match="설정과 다릅니다"):
        provision(setup)


def test_secret_validation_never_echoes_value(setup):
    setup[1]["secret://app"]["password"] = "PRIVATE\nSENTINEL"
    with pytest.raises(ProvisioningError) as e:
        provision(setup)
    assert "PRIVATE" not in str(e.value)


def test_missing_app_secret_prevents_deploy(setup):
    provision(setup)
    r = Runtime()
    with pytest.raises(ProvisioningError):
        deploy(setup, r, secret_refs={"API_KEY": "bad"})
    assert not r.calls


@pytest.mark.parametrize("part", ["db", "network", "app", "credentials_ref"])
def test_nested_target_contract_is_closed(setup, part):
    raw = provision(setup).target.model_dump()
    raw[part]["deployment_metadata"] = "must-not-leak"
    with pytest.raises(ValidationError):
        OnpremTarget.model_validate(raw)


def test_app_deploy_output_requires_same_target_url(setup):
    from app.features.provisioning.onprem.models import OnpremAppDeployOutput

    target = provision(setup).target
    with pytest.raises(ValidationError):
        OnpremAppDeployOutput(
            app_url="https://wrong.example.com",
            healthy=True,
            target=target,
            changes={"add": 1, "change": 0, "destroy": 0},
            applied=True,
        )


def test_existing_directory_cannot_be_adopted_or_modified(setup):
    root = Path(setup[0]["directory"])
    root.mkdir()
    (root / "compose.json").write_text("existing deployment")
    r = Runtime()
    with pytest.raises(ProvisioningError, match="자동 인수"):
        provision(setup, r)
    assert not r.calls and (root / "compose.json").read_text() == "existing deployment"


def test_documented_output_examples_and_json_schemas_match_code():
    from app.features.provisioning.onprem.models import (
        OnpremProvisioningOutput,
        OnpremAppDeployOutput,
    )

    docs = Path(__file__).resolve().parents[5] / "docs" / "onprem"
    for stem, model in [
        ("provision", OnpremProvisioningOutput),
        ("app-deploy", OnpremAppDeployOutput),
    ]:
        model.model_validate_json((docs / (stem + "-output.example.json")).read_text())
        assert (
            json.loads((docs / (stem + "-output.schema.json")).read_text())
            == model.model_json_schema()
        )
