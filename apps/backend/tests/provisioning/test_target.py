import json
from pathlib import Path

import pytest

from app.features.provisioning.target import (
    TargetBuildError,
    build_target,
    default_credentials_ref,
    find_problems,
    unwrap_outputs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "terraform_output.sample.json"


@pytest.fixture
def raw_output() -> dict:
    return json.loads(FIXTURE.read_text())


def make(raw: dict, **overrides) -> dict:
    args = {"run_id": "mig-001", "project_id": "example-project", "region": "asia-northeast3"}
    args.update(overrides)
    return build_target(raw, **args)


def test_target_has_the_shape_db_migration_expects(raw_output):
    # 안재현님 CDC 명세 2절: project_id/region, network.*, db.*, app.*, credentials_ref.*
    assert make(raw_output) == {
        "project_id": "example-project",
        "region": "asia-northeast3",
        "network": {
            "vpc_name": "target-app",
            "subnet_cidrs": ["10.10.0.0/24"],
            "psa_range_cidr": "10.244.0.0/16",
        },
        "db": {
            "instance": "target-app-db",
            "private_ip": "10.244.0.3",
            "port": 5432,
            "database": "workitems",
            "version": "POSTGRES_16",
            "ssl_mode": "ENCRYPTED_ONLY",
        },
        "app": {"url": None, "db_user": "alpha"},
        "credentials_ref": {
            "cloud": "secret://mig-001/target-cloud",
            "admin": "secret://mig-001/target-admin",
        },
    }


def test_secret_values_never_reach_the_target(raw_output):
    # 관리자 비밀번호와 VPN 키는 target 에 들어가면 안 된다. 이름표(secret://...)만 들어간다.
    text = json.dumps(make(raw_output))
    assert "FAKE-ADMIN-PASSWORD-DO-NOT-LEAK" not in text
    assert "FAKE-VPN-KEY-DO-NOT-LEAK" not in text


def test_app_url_is_filled_after_the_app_is_deployed(raw_output):
    raw_output["app_url"] = {"sensitive": False, "type": "string", "value": "https://target-app-abc.a.run.app"}
    assert make(raw_output)["app"]["url"] == "https://target-app-abc.a.run.app"


def test_accepts_plain_values_as_well_as_terraform_json(raw_output):
    plain = unwrap_outputs(raw_output)
    assert plain["db_port"] == 5432
    assert make(plain) == make(raw_output)


def test_credentials_ref_can_be_overridden_with_what_put_secret_returned(raw_output):
    refs = {"cloud": "secret://mig-001/vpn", "admin": "secret://mig-001/admin"}
    assert make(raw_output, credentials_ref=refs)["credentials_ref"] == refs


def test_default_credentials_ref_names():
    assert default_credentials_ref("mig-9") == {
        "cloud": "secret://mig-9/target-cloud",
        "admin": "secret://mig-9/target-admin",
    }


def test_missing_outputs_are_reported_by_name(raw_output):
    del raw_output["db_private_ip"]
    del raw_output["psa_range_cidr"]
    with pytest.raises(TargetBuildError) as error:
        make(raw_output)
    assert "db_private_ip" in str(error.value)
    assert "psa_range_cidr" in str(error.value)


def test_empty_value_counts_as_missing(raw_output):
    raw_output["db_private_ip"]["value"] = ""
    with pytest.raises(TargetBuildError):
        make(raw_output)


def test_a_good_target_has_no_problems(raw_output):
    assert find_problems(make(raw_output)) == []


def test_ssl_mode_other_than_encrypted_only_is_a_problem(raw_output):
    raw_output["db_ssl_mode"]["value"] = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    problems = find_problems(make(raw_output))
    assert any("ENCRYPTED_ONLY" in problem for problem in problems)


def test_postgres_older_than_16_is_a_problem(raw_output):
    raw_output["db_version"]["value"] = "POSTGRES_15"
    assert any("16" in problem for problem in find_problems(make(raw_output)))


@pytest.mark.parametrize(
    "field, cidr, peer",
    [
        ("psa_range_cidr", "10.50.0.0/16", "OCI"),  # 대역이 완전히 같음
        ("psa_range_cidr", "172.31.8.0/24", "AWS"),  # 상대 대역 안에 들어감
        ("subnet_cidrs", ["10.50.1.0/24"], "OCI"),
    ],
)
def test_overlap_with_oci_or_aws_is_a_problem(raw_output, field, cidr, peer):
    raw_output[field]["value"] = cidr
    problems = find_problems(make(raw_output))
    assert any(peer in problem for problem in problems), problems
