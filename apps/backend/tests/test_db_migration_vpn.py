"""The VPN order and values, with Terraform replaced by a fake that plays the clouds."""

import json
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.secrets import put_secret, resolve
from app.features.db_migration import vpn
from app.features.db_migration.schemas import DbMigrationInput

RUN_ID = "mig-20261105-001"
JOB_ID = "mig_20261105_001"
AWS_KEY = "AKIA-MARKER"
INPUT = {
    "run_id": RUN_ID,
    "source": {
        "account_id": "1", "region": "ap-northeast-2",
        "db": {"host": "rds.example", "database": "postgres"},
        "network": {"vpc_id": "vpc-1", "cidr": "172.31.0.0/16", "route_table_ids": ["rtb-1"],
                    "db_security_group_id": "sg-1"},
        "app": {"db_user": "alpha"}, "tables": ["public.work_items"],
        "credentials_ref": {"cloud": f"secret://{RUN_ID}/source-cloud", "db_admin": f"secret://{RUN_ID}/source-db-admin"},
    },
    "target": {
        "project_id": "proj", "region": "asia-northeast1",
        "db": {"instance": "i", "private_ip": "10.20.0.5", "database": "app"},
        "network": {"vpc_name": "vpc-a", "subnet_cidrs": ["10.10.0.0/24"], "psa_range_cidr": "10.20.0.0/20"},
        "app": {"db_user": "app_user"},
        "credentials_ref": {"cloud": f"secret://{RUN_ID}/target-cloud", "admin": f"secret://{RUN_ID}/target-admin",
                            "ddl_user": f"secret://{RUN_ID}/target-ddl-user",
                            "sink_user": f"secret://{RUN_ID}/target-sink-user"},
    },
}
OCI_IPS = {"aws-0": "9.0.0.1", "aws-1": "9.0.0.2", "gcp-0": "9.0.0.3", "gcp-1": "9.0.0.4"}


class FakeClouds:
    """Records every terraform call and answers outputs as applied resources would."""

    def __init__(self, status="UP", aws_ips_after_swap=None):
        self.applies: list[tuple[str, dict]] = []
        self.commands: list[list[str]] = []
        self.destroyed: list[str] = []
        self.refreshes = 0
        self.fail_destroy: set[str] = set()
        self.state: dict[str, dict] = {"gcp": {}, "aws-vpn": {}, "oci-vpn": {}}
        self.status = status
        self.aws_ips_after_swap = aws_ips_after_swap
        self.envs: dict[str, dict] = {}
        self.file_modes: list[int] = []

    def __call__(self, command, env, capture_output, text, timeout):
        root = Path(command[1].removeprefix("-chdir=")).name
        action = command[2]
        self.commands.append(command)
        self.envs[root] = env
        out = ""
        if action in ("apply", "destroy"):
            path = Path(next(a for a in command if a.startswith("-var-file=")).removeprefix("-var-file="))
            self.file_modes.append(stat.S_IMODE(path.stat().st_mode))
            tfvars = json.loads(path.read_text())
            if action == "destroy":
                if root in self.fail_destroy:
                    return subprocess.CompletedProcess(command, 1, "", "boom")
                self.destroyed.append(root)
                self.state[root] = {}
            elif "-refresh-only" in command:
                self.refreshes += 1
            else:
                self.applies.append((root, tfvars))
                self._apply(root, tfvars)
        elif action == "output":
            out = json.dumps({k: {"value": v} for k, v in self.state[root].items()})
        return subprocess.CompletedProcess(command, 0, out, "")

    def _apply(self, root, tfvars):
        if root == "gcp":
            self.state[root] = {"ha_vpn_interface_ips": ["8.0.0.1", "8.0.0.2"]}
        elif root == "aws-vpn":
            swapped = tfvars["tunnels"][0]["oci_ip"] not in vpn.PLACEHOLDER_IPS
            ips = (swapped and self.aws_ips_after_swap) or ["7.0.0.1", "7.0.0.2"]
            self.state[root] = {"tunnels": [{"aws_ip": ip} for ip in ips]}
        else:
            self.state[root] = {"oracle_tunnel_ips": OCI_IPS, "tunnel_status": {k: self.status for k in OCI_IPS}}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CDC_OCI_COMPARTMENT_ID", "ocid-compartment")
    monkeypatch.setenv("CDC_OCI_DRG_ID", "ocid-drg")
    monkeypatch.setenv("CDC_TFSTATE_BUCKET", "bucket")
    monkeypatch.setenv("CDC_TFSTATE_NAMESPACE", "ns")
    monkeypatch.setenv("AWS_PROFILE", "developer-own-login")


def make_tf(clouds: FakeClouds) -> vpn.Terraform:
    return vpn.Terraform(vpn.OciSettings.from_env(), JOB_ID,
                         {"aws_access_key_id": AWS_KEY, "aws_secret_access_key": "s"},
                         {"credentials_json": "{}"}, runner=clouds)


def ensure(clouds: FakeClouds) -> dict:
    return vpn.ensure_vpn(DbMigrationInput.model_validate(INPUT), JOB_ID, terraform=make_tf(clouds),
                          sleep=lambda s: None)


def test_order_follows_the_ip_dependencies():
    clouds = FakeClouds()
    result = ensure(clouds)
    gcp1, aws1, oci, aws2, gcp2 = clouds.applies

    assert [a[0] for a in clouds.applies] == ["gcp", "aws-vpn", "oci-vpn", "aws-vpn", "gcp"]
    assert gcp1[1]["oci_tunnels"] == [] and gcp1[1]["shared_secrets"] == []
    assert [t["oci_ip"] for t in aws1[1]["tunnels"]] == vpn.PLACEHOLDER_IPS
    assert oci[1]["tunnels"]["aws-0"]["cpe_ip"] == "7.0.0.1"
    assert oci[1]["tunnels"]["gcp-1"]["cpe_ip"] == "8.0.0.2"
    assert [t["oci_ip"] for t in aws2[1]["tunnels"]] == ["9.0.0.1", "9.0.0.2"]
    assert [t["oci_ip"] for t in gcp2[1]["oci_tunnels"]] == ["9.0.0.3", "9.0.0.4"]
    assert result["tunnel_status"]["gcp-0"] == "UP"


def test_input_values_reach_the_right_roots():
    clouds = FakeClouds()
    ensure(clouds)
    gcp, aws, oci = clouds.applies[4][1], clouds.applies[3][1], clouds.applies[2][1]

    assert (gcp["project"], gcp["region"], gcp["network"]) == ("proj", "asia-northeast1", "vpc-a")
    assert gcp["advertised_ip_ranges"] == ["10.20.0.0/20"]
    assert (aws["vpc_id"], aws["route_table_ids"], aws["db_security_group_id"]) == ("vpc-1", ["rtb-1"], "sg-1")
    assert oci["drg_id"] == "ocid-drg"
    assert oci["tunnels"]["aws-0"]["peer_cidrs"] == ["172.31.0.0/16"]
    assert oci["tunnels"]["gcp-0"]["peer_cidrs"] == ["10.10.0.0/24", "10.20.0.0/20"]


def test_both_ends_of_a_tunnel_agree():
    clouds = FakeClouds()
    ensure(clouds)
    gcp, aws, oci = clouds.applies[4][1], clouds.applies[3][1], clouds.applies[2][1]
    tunnels = oci["tunnels"]

    assert aws["tunnels"][0]["inside_cidr"] == "169.254.21.0/30"
    assert tunnels["aws-0"]["oracle_bgp_ip"] == "169.254.21.2/30"
    assert tunnels["aws-0"]["peer_bgp_ip"] == "169.254.21.1/30"
    assert gcp["oci_tunnels"][1]["gcp_bgp_ip"] == tunnels["gcp-1"]["peer_bgp_ip"]
    assert gcp["oci_tunnels"][1]["oci_bgp_ip"] + "/30" == tunnels["gcp-1"]["oracle_bgp_ip"]
    assert aws["shared_secrets"] == [oci["shared_secrets"]["aws-0"], oci["shared_secrets"]["aws-1"]]
    assert gcp["shared_secrets"] == [oci["shared_secrets"]["gcp-0"], oci["shared_secrets"]["gcp-1"]]


def test_bgp_addresses_avoid_the_range_oci_refuses():
    for net in vpn.BGP_NETS.values():
        assert not 10 <= int(net.split(".")[2]) <= 19


def test_keys_go_only_through_environment_and_only_to_their_cloud():
    clouds = FakeClouds()
    ensure(clouds)

    assert clouds.envs["aws-vpn"]["AWS_ACCESS_KEY_ID"] == AWS_KEY
    assert "AWS_ACCESS_KEY_ID" not in clouds.envs["gcp"]
    assert "GOOGLE_CREDENTIALS" not in clouds.envs["aws-vpn"]
    assert "AWS_PROFILE" not in clouds.envs["aws-vpn"]
    assert not any(AWS_KEY in " ".join(c) for c in clouds.commands)
    assert all(m == 0o600 for m in clouds.file_modes)


def test_session_token_is_passed_only_for_temporary_credentials():
    permanent = vpn.Terraform(vpn.OciSettings.from_env(), JOB_ID,
                              {"aws_access_key_id": AWS_KEY, "aws_secret_access_key": "s"},
                              {"credentials_json": "{}"})
    temporary = vpn.Terraform(vpn.OciSettings.from_env(), JOB_ID,
                              {"aws_access_key_id": AWS_KEY, "aws_secret_access_key": "s", "aws_session_token": "t"},
                              {"credentials_json": "{}"})

    assert "AWS_SESSION_TOKEN" not in permanent.cloud_env[vpn.AWS]
    assert temporary.cloud_env[vpn.AWS]["AWS_SESSION_TOKEN"] == "t"


def test_tfvars_file_is_gone_after_each_command(tmp_path):
    ensure(FakeClouds())
    assert list(tmp_path.rglob("run.tfvars.json")) == []


def test_state_is_kept_per_run_and_root():
    clouds = FakeClouds()
    ensure(clouds)
    inits = [c for c in clouds.commands if c[2] == "init"]

    assert len(inits) == 3
    assert any(f"-backend-config=key={JOB_ID}/oci-vpn.tfstate" in c for c in inits)
    assert all("-backend-config=bucket=bucket" in c and "-backend-config=namespace=ns" in c for c in inits)


def test_rerun_with_healthy_tunnels_applies_nothing():
    clouds = FakeClouds()
    assert ensure(clouds)["skipped"] is False
    clouds.applies.clear()
    clouds.refreshes = 0

    result = ensure(clouds)

    assert result["skipped"] is True
    assert result["tunnel_status"] == {k: "UP" for k in OCI_IPS}
    assert clouds.applies == []
    assert clouds.refreshes == 1


def test_rerun_with_a_tunnel_down_rebuilds():
    clouds = FakeClouds()
    ensure(clouds)
    clouds.state["oci-vpn"]["tunnel_status"]["gcp-1"] = "DOWN"
    clouds.applies.clear()

    assert ensure(clouds)["skipped"] is False
    assert clouds.applies


def test_rerun_with_changed_input_rebuilds():
    clouds = FakeClouds()
    ensure(clouds)
    clouds.applies.clear()
    changed = json.loads(json.dumps(INPUT))
    changed["source"]["network"]["cidr"] = "172.32.0.0/16"

    result = vpn.ensure_vpn(DbMigrationInput.model_validate(changed), JOB_ID, terraform=make_tf(clouds),
                            sleep=lambda s: None)

    assert result["skipped"] is False
    assert clouds.applies


def test_run_interrupted_before_any_tunnel_rebuilds():
    clouds = FakeClouds()
    ensure(clouds)
    clouds.state["oci-vpn"] = {}
    clouds.applies.clear()

    assert ensure(clouds)["skipped"] is False
    assert [a[0] for a in clouds.applies] == ["gcp", "aws-vpn", "oci-vpn", "aws-vpn", "gcp"]


def test_rerun_keeps_the_tunnels_and_the_same_secrets():
    clouds = FakeClouds()
    ensure(clouds)
    first_secrets = clouds.applies[2][1]["shared_secrets"]
    clouds.applies.clear()
    clouds.state["oci-vpn"]["tunnel_status"]["aws-0"] = "DOWN"
    ensure(clouds)

    # no placeholder gateways on a rerun
    assert all(a[1]["oci_tunnels"] for a in clouds.applies if a[0] == "gcp")
    assert all(a[1]["tunnels"][0]["oci_ip"] == "9.0.0.1" for a in clouds.applies if a[0] == "aws-vpn")
    assert clouds.applies[2][1]["shared_secrets"] == first_secrets


def test_aws_ip_change_stops_with_a_clear_error():
    with pytest.raises(RuntimeError, match="AWS tunnel IPs changed"):
        ensure(FakeClouds(aws_ips_after_swap=["7.9.9.1", "7.9.9.2"]))


def test_tunnels_that_never_come_up_fail(monkeypatch):
    monkeypatch.setattr(vpn, "TUNNEL_WAIT_S", -1)
    clouds = FakeClouds(status="DOWN")
    with pytest.raises(RuntimeError, match="not up"):
        ensure(clouds)
    assert clouds.refreshes == 1


def test_terraform_failure_names_the_root():
    clouds = FakeClouds()
    clouds.fail_destroy = {"gcp"}
    ensure(clouds)
    with pytest.raises(vpn.TerraformError, match="gcp"):
        vpn.destroy_vpn(RUN_ID, terraform=make_tf(clouds))


def test_destroy_removes_all_three_with_only_the_run_id():
    clouds = FakeClouds()
    ensure(clouds)

    assert vpn.destroy_vpn(RUN_ID, terraform=make_tf(clouds)) is True
    assert clouds.destroyed == ["gcp", "aws-vpn", "oci-vpn"]


def test_destroy_goes_on_after_one_failure_and_can_run_again():
    clouds = FakeClouds()
    ensure(clouds)
    clouds.fail_destroy = {"aws-vpn"}
    with pytest.raises(vpn.TerraformError):
        vpn.destroy_vpn(RUN_ID, terraform=make_tf(clouds))
    assert clouds.destroyed == ["gcp", "oci-vpn"]

    clouds.fail_destroy = set()
    assert vpn.destroy_vpn(RUN_ID, terraform=make_tf(clouds)) is True
    assert clouds.destroyed[-1] == "oci-vpn" and "aws-vpn" in clouds.destroyed


def test_destroy_without_a_vpn_does_nothing():
    clouds = FakeClouds()
    assert vpn.destroy_vpn("mig-never-ran", terraform=make_tf(clouds)) is False
    assert clouds.commands == []


def test_saved_context_has_references_not_values():
    put_secret(f"{RUN_ID}/source-cloud", {"aws_access_key_id": AWS_KEY, "aws_secret_access_key": "s"})
    ensure(FakeClouds())
    saved = resolve(f"secret://{RUN_ID}/vpn-context")["json"]

    assert f"secret://{RUN_ID}/source-cloud" in saved and AWS_KEY not in saved
