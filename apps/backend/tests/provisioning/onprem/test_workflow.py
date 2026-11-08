from types import SimpleNamespace

import pytest

from app.features.workflows import runners, target_adapters
from app.features.workflows.schemas import MigrationRequest, TargetInfraConfig
from app.features.provisioning.onprem import service
from app.features.db_migration.schemas import DbMigrationInput

SOURCE = {
    "account_id": "123456789012",
    "region": "ap-northeast-2",
    "db": {"host": "rds.internal", "database": "postgres"},
    "network": {
        "vpc_id": "vpc-test",
        "cidr": "172.31.0.0/16",
        "route_table_ids": ["rtb-test"],
    },
    "app": {"db_user": "app"},
    "tables": ["public.work_items", "alpha_cdc.cutover_marker"],
    "credentials_ref": {"cloud": "secret://aws", "db_admin": "secret://admin"},
}
TARGET = {
    "cloud": "onprem",
    "region": "local",
    "project_id": None,
    "network": {},
    "db": {
        "instance": "local",
        "private_ip": "127.0.0.1",
        "port": 15432,
        "database": "appdb",
    },
    "app": {"url": None, "db_user": "app"},
    "credentials_ref": {"cloud": "secret://config", "admin": "secret://admin"},
}


def test_registered_adapter_forwards_existing_arguments_and_reference(monkeypatch):
    called = {}
    monkeypatch.setattr(
        service, "provision", lambda **kw: called.update(kw) or {"target": TARGET}
    )
    cfg = TargetInfraConfig(
        provider="onprem", extra_params={"onprem_config_ref": "secret://config"}
    )
    request = MigrationRequest(
        service_name="app", aws_region="ap-northeast-2", target_infra=cfg
    )
    adapter = target_adapters.adapter_for(request)
    assert adapter.provider == "onprem"
    assert adapter.provision("run-1", {}, cfg, None) == {"target": TARGET}
    assert called["config_ref"] == "secret://config"


@pytest.mark.parametrize("existing_vpn", [False, True])
def test_runner_passes_target_unchanged_and_selects_onprem_vpn(monkeypatch, existing_vpn):
    from app.features.db_migration import service as db_service

    captured = {}

    def run(inp, ctx):
        captured.update(inp)
        DbMigrationInput.model_validate(inp)
        return {"state": "streaming", "phase": "streaming", "job_id": "run_1"}

    monkeypatch.setattr(db_service, "run", run)
    monkeypatch.setattr(runners, "_require_real_agent", lambda: None)
    monkeypatch.setattr(runners, "_use_existing_vpn", lambda: existing_vpn)
    request = MigrationRequest(
        service_name="app",
        aws_region="ap-northeast-2",
        target_infra={
            "provider": "onprem",
            "extra_params": {"onprem_config_ref": "secret://config"},
        },
    )
    run = SimpleNamespace(
        run_id="run-1",
        request=request,
        outputs={"planning": {"source": SOURCE}, "provisioning": {"target": TARGET}},
    )
    result = runners.run_db_migration(run, None, poll_interval_s=0.001, timeout_s=1)
    assert result["state"] == "streaming"
    assert captured["target"] == TARGET
    assert captured["options"] == ({"pass_onprem": True, "skip_vpn": True} if existing_vpn else {"pass_onprem": True})
    assert captured["source"]["tables"] == ["public.work_items"]


def test_onprem_selection_uses_operator_config_without_extra_ui_fields(monkeypatch):
    monkeypatch.setenv("ONPREM_CONFIG_REF", "secret://operator/onprem")
    request = MigrationRequest(service_name="app", aws_region="ap-northeast-2", target_infra={"provider": "onprem"})
    adapter = target_adapters.adapter_for(request)
    assert adapter._ref(request.target_infra) == "secret://operator/onprem"


def test_missing_operator_config_fails_before_provisioning(monkeypatch):
    monkeypatch.delenv("ONPREM_CONFIG_REF", raising=False)
    request = MigrationRequest(service_name="app", aws_region="ap-northeast-2", target_infra={"provider": "onprem"})
    with pytest.raises(target_adapters.UnsupportedTarget, match="ONPREM_CONFIG_REF"):
        target_adapters.adapter_for(request).provision("run-1", {}, request.target_infra, None)


@pytest.mark.parametrize("output", [{}, {"worker_target_updated": False}])
def test_onprem_cannot_cutover_without_worker_target_registration(output):
    request = MigrationRequest(service_name="app", aws_region="ap-northeast-2", target_infra={"provider": "onprem"})
    run = SimpleNamespace(request=request, outputs={"app_deploy": output})
    with pytest.raises(RuntimeError, match="Worker 목적지 등록"):
        runners.run_cutover(run, None)


def test_planning_recognizes_onprem_without_claiming_cloud_terraform():
    from app.features.assessment.schemas import TargetSelection
    from app.features.assessment import rules, targets
    assert TargetSelection(cloud="onprem", region="local").cloud == "onprem"
    assert targets.profile("onprem").app_platform == "Docker WAS"
    steps = rules.build_steps(cloud="onprem")
    assert any("Docker Compose" in step.action for step in steps)
    assert not any("terraform apply" in step.action for step in steps)
