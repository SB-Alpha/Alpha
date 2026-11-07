"""공통 모델의 정보 보존, 단계 경계, 입력 불일치와 부수 효과 없는 변환을 검증한다."""

import copy
import os
from dataclasses import replace
from itertools import permutations

import pytest

from app.core import secrets
from app.features.db_migration import cloud_lookup, settings
from app.features.db_migration.cloud_lookup import FoundDb, FoundEndpoint, FoundRoute
from app.features.db_migration.execution_model import ExecutionModelError, build_execution_model
from app.features.db_migration.run_input import InjectedEndpoint, InjectedRun
from app.features.db_migration.start_checks import CdcNetwork, StartCheckError, StartCheckInput, check_start

REGIONS = {"aws": "ap-northeast-2", "gcp": "asia-northeast3", "azure": "koreacentral"}
PROVIDER_FIELDS = {
    "aws": {"vpc_id": "vpc-1", "route_table_ids": ["rtb-1"], "db_security_group_ids": ["sg-1"]},
    "gcp": {"project_id": "host-project", "vpc_name": "net", "psa_range_cidr": "10.20.0.0/20"},
    "azure": {"resource_group": "rg", "vnet_name": "net", "address_space": ["10.30.0.0/16"],
              "has_gateway_subnet": False},
}
SECRET = "do-not-echo-this-value"


def inputs(source="aws", target="gcp"):
    def injected(cloud, side):
        return InjectedEndpoint(cloud, REGIONS[cloud], f"secret://cloud/{cloud}", f"secret://mig-001/{side}-admin")

    def found(cloud, side):
        return FoundEndpoint(
            cloud, REGIONS[cloud], f"{cloud}-scope",
            FoundDb("instance", f"{side}.example", 5432, "16.4", False, side == "source"),
            ("172.31.0.0/16",) if side == "source" else ("10.20.0.0/16",),
            copy.deepcopy(PROVIDER_FIELDS[cloud]),
        )

    return (
        InjectedRun("mig-001", injected(source, "source"), injected(target, "target"), "target_app"),
        FoundRoute(found(source, "source"), found(target, "target")),
    )


@pytest.mark.parametrize("source,target", tuple(permutations(REGIONS, 2)))
def test_six_routes_preserve_common_and_provider_values(source, target):
    injected, found = inputs(source, target)
    model = build_execution_model(injected, found)
    assert model.run_id == injected.run_id
    assert model.target_app_user == injected.target_app_user
    assert model.source.model_dump().keys() == model.target.model_dump().keys()
    for side in ("source", "target"):
        endpoint, result = getattr(found, side), getattr(model, side)
        refs = getattr(injected, side)
        assert (result.cloud, result.region, result.scope_id) == (endpoint.cloud, endpoint.region, endpoint.scope_id)
        assert result.db.model_dump() == vars(endpoint.db)
        assert result.network.cidrs == endpoint.cidrs
        assert result.network.provider_fields == endpoint.network
        assert result.credentials_ref.cloud == refs.credentials_ref
        assert result.credentials_ref.db_admin == refs.db_admin_ref
        assert "database" not in result.db.model_dump()
        assert "tables" not in result.model_dump()
    # 시작 전 검사가 새 모델의 공통 필드만으로 동작한다.
    common = StartCheckInput.from_route(model)
    assert common.source_database is None
    assert common.source_cidrs == found.source.cidrs
    assert common.target_cidrs == found.target.cidrs
    agent = type("Agent", (), {"status": lambda self: {"kafka": True, "connect": True, "plugins_missing": []},
                               "list_jobs": lambda self: []})()
    check_start(common, job_id="mig_001", agent=agent, network=CdcNetwork("10.50.0.0/16", "10.50.1.0/24"))


def test_provider_fields_are_copied_including_nested_lists():
    injected, found = inputs()
    model = build_execution_model(injected, found)
    found.source.network["route_table_ids"].append("changed-input")
    model.source.network.provider_fields["db_security_group_ids"].append("changed-model")
    assert model.source.network.provider_fields["route_table_ids"] == ["rtb-1"]
    assert found.source.network["db_security_group_ids"] == ["sg-1"]


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("field,value", [("cloud", "azure"), ("region", "different-region")])
def test_lookup_from_another_request_is_rejected(side, field, value):
    injected, found = inputs()
    found = replace(found, **{side: replace(getattr(found, side), **{field: value})})
    with pytest.raises(ExecutionModelError, match=f"{side}: lookup does not match"):
        build_execution_model(injected, found)


def test_same_cloud_is_not_admitted_by_internal_conversion():
    injected, found = inputs("aws", "aws")
    with pytest.raises(ExecutionModelError, match="different clouds"):
        build_execution_model(injected, found)


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("field,value", [("port", 0), ("port", True), ("host", ""), ("instance", ""),
                                       ("version", f"{SECRET}\n"), ("publicly_accessible", "false")])
def test_incomplete_or_wrongly_typed_db_is_rejected_without_echoing_values(side, field, value):
    injected, found = inputs()
    endpoint = getattr(found, side)
    found = replace(found, **{side: replace(endpoint, db=replace(endpoint.db, **{field: value}))})
    with pytest.raises(ExecutionModelError) as exc:
        build_execution_model(injected, found)
    assert str(exc.value).startswith(f"{side}:")
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("cloud,region", [("unknown", "region"), ("aws", SECRET)])
def test_invalid_cloud_or_region_is_reported_without_values(cloud, region):
    injected, found = inputs()
    injected = replace(injected, source=replace(injected.source, cloud=cloud, region=region))
    found = replace(found, source=replace(found.source, cloud=cloud, region=region))
    with pytest.raises(ExecutionModelError) as exc:
        build_execution_model(injected, found)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("field", ["credentials_ref", "db_admin_ref"])
def test_only_references_are_accepted_for_credentials(field):
    injected, found = inputs()
    injected = replace(injected, source=replace(injected.source, **{field: SECRET}))
    with pytest.raises(ExecutionModelError) as exc:
        build_execution_model(injected, found)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("field,value", [("run_id", f"{SECRET}\n"), ("target_app_user", f"{SECRET}\n")])
def test_execution_identity_errors_do_not_echo_values(field, value):
    injected, found = inputs()
    with pytest.raises(ExecutionModelError) as exc:
        build_execution_model(replace(injected, **{field: value}), found)
    assert SECRET not in str(exc.value)


def test_unknown_db_facts_and_empty_provider_metadata_are_preserved():
    injected, found = inputs()
    found = replace(found, source=replace(found.source, network={}, db=replace(
        found.source.db, version=None, publicly_accessible=None, logical_replication=None)))
    model = build_execution_model(injected, found)
    assert model.source.db.version is None
    assert model.source.db.publicly_accessible is None
    assert model.source.db.logical_replication is None
    assert model.source.network.provider_fields == {}


def test_missing_cidrs_or_non_json_provider_values_are_rejected():
    injected, found = inputs()
    for endpoint in (replace(found.source, cidrs=()), replace(found.source, network={SECRET: object()})):
        with pytest.raises(ExecutionModelError) as exc:
            build_execution_model(injected, replace(found, source=endpoint))
        assert SECRET not in str(exc.value)


def test_public_access_policy_is_left_to_start_checks():
    injected, found = inputs()
    found = replace(found, source=replace(found.source, db=replace(found.source.db, publicly_accessible=True)))
    model = build_execution_model(injected, found)
    with pytest.raises(StartCheckError, match="public access"):
        check_start(StartCheckInput.from_route(model), job_id="mig_001", agent=None,
                    network=CdcNetwork("10.50.0.0/16", "10.50.1.0/24"))


def test_conversion_reads_no_environment_secrets_or_cloud_api(monkeypatch):
    injected, found = inputs()

    def forbidden(*args, **kwargs):
        raise AssertionError("conversion must not perform IO or read environment settings")

    with monkeypatch.context() as isolated:
        isolated.setattr(os, "getenv", forbidden)
        isolated.setattr(secrets, "resolve", forbidden)
        isolated.setattr(secrets, "put_secret", forbidden)
        isolated.setattr(settings, "tunables", forbidden)
        isolated.setattr(cloud_lookup, "find_route", forbidden)
        model = build_execution_model(injected, found)
    assert model.source.db.host == found.source.db.host
    assert model.target.db.host == found.target.db.host
