"""클라우드 간 순서·역할, 재사용·부분 실패·정리와 Terraform 비밀값 경계를 검증한다."""

import copy
import ipaddress
import json
import stat
import subprocess
from dataclasses import replace
from itertools import combinations, permutations

import pytest
from cryptography.fernet import Fernet

from app.core.secrets import delete_secret, has_secret, put_secret, resolve
from app.features.db_migration import vpn, vpn_workflow as flow
from app.features.db_migration.vpn_peers import ADAPTERS
from app.features.db_migration.vpn_plan import BGP_NETWORKS, TUNNEL_KEYS, VpnError, VpnLeg, VpnPlan
from app.features.db_migration.vpn_terraform import TerraformSession

SECRET = "private-key-do-not-echo"
RUN_ID, JOB_ID = "mig-vpn-001", "mig_vpn_001"
OCI_IPS = {key: f"9.0.0.{i + 1}" for i, key in enumerate(TUNNEL_KEYS)}


def test_bgp_networks_fit_every_cloud_and_do_not_overlap():
    networks = [ipaddress.ip_network(cidr) for pair in BGP_NETWORKS.values() for cidr in pair]
    azure_low, azure_high = ipaddress.ip_address("169.254.21.0"), ipaddress.ip_address("169.254.22.255")
    oci_low, oci_high = ipaddress.ip_address("169.254.10.0"), ipaddress.ip_address("169.254.19.255")

    assert len(networks) == 4 and all(net.prefixlen == 30 for net in networks)
    assert all(azure_low <= net[0] and net[-1] <= azure_high for net in networks)
    assert not any(oci_low <= net[-1] and net[0] <= oci_high for net in networks)
    assert not any(a.overlaps(b) for a, b in combinations(networks, 2))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    for name, value in {
        "CDC_OCI_COMPARTMENT_ID": "compartment", "CDC_OCI_DRG_ID": "drg", "CDC_TFSTATE_BUCKET": "bucket",
        "CDC_TFSTATE_NAMESPACE": "namespace", "CDC_OCI_REGION": "oci-region", "CDC_OCI_VCN_CIDR": "10.50.0.0/16",
        "CDC_NODE_SUBNET_CIDR": "10.50.1.0/24",
    }.items():
        monkeypatch.setenv(name, value)
    put_secret("cloud/aws", {"aws_access_key_id": "key", "aws_secret_access_key": SECRET})
    put_secret("cloud/gcp", {"credentials_json": SECRET})
    put_secret("cloud/azure", {"tenant_id": "tenant", "client_id": "client", "client_secret": SECRET, "subscription_id": "sub"})


def leg(role, cloud):
    networks = {
        "aws": {"vpc_id": "vpc", "route_table_ids": ["rtb"], "db_security_group_id": "sg"},
        "gcp": {"project_id": "network-project", "vpc_name": "net", "psa_range_cidr": "10.20.0.0/20",
                "psa_range_cidrs": ["10.20.0.0/20", "10.21.0.0/20"]},
        "azure": {"resource_group": "rg", "vnet_name": "net", "has_gateway_subnet": True},
    }
    return VpnLeg(role, cloud, "region", "scope", ("172.31.0.0/16",) if cloud == "aws" else ("10.20.0.0/16",),
                  copy.deepcopy(networks[cloud]), 6543, f"secret://cloud/{cloud}")


def plan(source="aws", target="gcp"):
    return VpnPlan(RUN_ID, JOB_ID, leg("source", source), leg("target", target))


class FakeTerraform:
    def __init__(self, plan, *, down=False):
        self.plan = plan
        self.state = {"source": {}, "target": {}, "oci": {}}
        self.applies, self.destroyed, self.refreshes = [], [], []
        self.fail_apply = None
        self.fail_destroy = set()
        self.down = down
        self.change_ip_on_connect = False

    def apply(self, role, values):
        self.applies.append((role, values))
        if role == self.fail_apply:
            raise VpnError("terraform", "apply failed")
        if role == "oci":
            self.state[role] = {"oracle_tunnel_ips": OCI_IPS, "tunnel_status": {key: "DOWN" if self.down else "UP" for key in TUNNEL_KEYS}}
            return
        endpoint = getattr(self.plan, role)
        ips = ["7.0.0.1", "7.0.0.2"] if role == "source" else ["8.0.0.1", "8.0.0.2"]
        if self.change_ip_on_connect and any(r == "oci" for r, _ in self.applies):
            ips[0] = "7.9.9.1"
        if endpoint.cloud == "aws":
            self.state[role] = {"tunnels": [{"aws_ip": ips[i], "aws_bgp_ip": endpoint.peer_bgp_ip(i),
                                             "oci_bgp_ip": endpoint.oracle_bgp_ip(i), "amazon_asn": 64512} for i in range(2)]}
        elif endpoint.cloud == "azure":
            self.state[role] = {"gateway_public_ips": ips, "gateway_asn": 65515,
                                "gateway_bgp_ips": [endpoint.peer_bgp_ip(i) for i in range(2)]}
        else:
            self.state[role] = {"ha_vpn_interface_ips": ips, "router_asn": 65010}

    def output(self, role, name):
        return self.state[role].get(name)

    def refresh(self, role, values):
        self.refreshes.append((role, values))

    def destroy(self, role, values):
        self.destroyed.append((role, values))
        if role in self.fail_destroy:
            raise RuntimeError(SECRET)
        self.state[role] = {}


@pytest.mark.parametrize("source,target", [("aws", "gcp"), ("gcp", "aws")])
def test_aws_and_gcp_run_in_either_role_with_the_same_order(source, target):
    inp = plan(source, target)
    tf = FakeTerraform(inp)
    result = flow.ensure_vpn(inp, terraform=tf)
    assert [role for role, _ in tf.applies] == ["source", "target", "oci", "source", "target"]
    assert result == {"name": inp.name, "tunnel_status": {key: "UP" for key in TUNNEL_KEYS}, "skipped": False}
    source_values, target_values, oci = (item[1] for item in tf.applies[:3])
    assert set(oci["tunnels"]) == set(TUNNEL_KEYS)
    assert oci["tunnels"]["source-0"]["peer_asn"] == ("64512" if source == "aws" else "65010")
    for role, values in tf.applies[3:]:
        endpoint = getattr(inp, role)
        assert values["shared_secrets"] == [oci["shared_secrets"][key] for key in endpoint.keys]
        if endpoint.cloud == "aws":
            assert values["db_port"] == 6543 and values["oci_cidr"] == "10.50.1.0/24"
            assert [t["oci_ip"] for t in values["tunnels"]] == [OCI_IPS[key] for key in endpoint.keys]
        else:
            assert values["advertised_ip_ranges"] == ["10.20.0.0/20", "10.21.0.0/20"]
            assert [t["oci_ip"] for t in values["oci_tunnels"]] == [OCI_IPS[key] for key in endpoint.keys]
        for i, key in enumerate(endpoint.keys):
            assert oci["tunnels"][key]["oracle_bgp_ip"] == f"{endpoint.oracle_bgp_ip(i)}/30"
            assert oci["tunnels"][key]["peer_bgp_ip"] == f"{endpoint.peer_bgp_ip(i)}/30"


def test_healthy_rerun_reuses_live_state_and_psk():
    inp = plan()
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    keys = resolve(f"secret://{RUN_ID}/vpn-psk")
    tf.applies.clear()
    assert flow.ensure_vpn(inp, terraform=tf)["skipped"] is True
    assert tf.applies == [] and resolve(f"secret://{RUN_ID}/vpn-psk") == keys


def test_down_tunnels_reconnect_using_existing_gateway_ips():
    inp = plan()
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    tf.state["oci"]["tunnel_status"]["source-0"] = "DOWN"
    tf.applies.clear()
    assert flow.ensure_vpn(inp, terraform=tf)["skipped"] is False
    assert tf.applies[0][1]["tunnels"][0]["oci_ip"] == OCI_IPS["source-0"]


@pytest.mark.parametrize("role", ["source", "target", "oci"])
def test_partial_apply_failure_retains_context_and_keys_for_cleanup(role):
    inp = plan()
    tf = FakeTerraform(inp)
    tf.fail_apply = role
    with pytest.raises(VpnError):
        flow.ensure_vpn(inp, terraform=tf)
    assert has_secret(f"secret://{RUN_ID}/vpn-context") and has_secret(f"secret://{RUN_ID}/vpn-psk")
    flow.destroy_vpn(RUN_ID, terraform=tf)
    assert [role for role, _ in tf.destroyed] == ["target", "source", "oci"]


def test_cleanup_continues_after_failure_and_can_be_retried():
    inp = plan()
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    tf.fail_destroy = {"source"}
    with pytest.raises(VpnError, match="source") as exc:
        flow.destroy_vpn(RUN_ID, terraform=tf)
    assert SECRET not in str(exc.value)
    assert [role for role, _ in tf.destroyed] == ["target", "source", "oci"]
    tf.fail_destroy.clear()
    assert flow.destroy_vpn(RUN_ID, terraform=tf)


def test_existing_cleanup_entry_dispatches_new_context():
    inp = plan()
    tf = FakeTerraform(inp)
    vpn.ensure_vpn(inp, JOB_ID, terraform=tf)
    assert vpn.destroy_vpn(RUN_ID, terraform=tf)
    assert [role for role, _ in tf.destroyed] == ["target", "source", "oci"]


def test_gateway_ip_change_is_not_reported_as_success():
    inp = plan()
    tf = FakeTerraform(inp)
    tf.change_ip_on_connect = True
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "peer_changed"


def test_timeout_keeps_cleanup_context(monkeypatch):
    monkeypatch.setenv("CDC_TUNNEL_WAIT_S", "-1")
    inp = plan()
    tf = FakeTerraform(inp, down=True)
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "tunnel_timeout" and has_secret(f"secret://{RUN_ID}/vpn-context")


def test_changed_plan_cannot_overwrite_existing_resources():
    inp = plan()
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    before = resolve(f"secret://{RUN_ID}/vpn-context")
    changed = replace(inp, source=replace(inp.source, cidrs=("172.30.0.0/16",)))
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(changed, terraform=tf)
    assert exc.value.code == "context_changed" and resolve(f"secret://{RUN_ID}/vpn-context") == before


def test_lost_psk_is_not_regenerated_for_an_existing_vpn():
    inp = plan()
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    delete_secret(f"secret://{RUN_ID}/vpn-psk")
    for operation in (lambda: flow.ensure_vpn(inp, terraform=tf), lambda: flow.destroy_vpn(RUN_ID, terraform=tf)):
        with pytest.raises(VpnError) as exc:
            operation()
        assert exc.value.code == "missing_psk"
    assert not has_secret(f"secret://{RUN_ID}/vpn-psk")


@pytest.mark.parametrize("source,target", [("azure", "aws"), ("gcp", "azure")])
def test_missing_adapter_is_rejected_before_any_resource_or_secret(source, target):
    inp = plan(source, target)
    tf = FakeTerraform(inp)
    registry = {cloud: adapter for cloud, adapter in ADAPTERS.items() if cloud != "azure"}
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf, registry=registry)
    assert exc.value.code == "unsupported_cloud" and not tf.applies
    assert not has_secret(f"secret://{RUN_ID}/vpn-context") and not has_secret(f"secret://{RUN_ID}/vpn-psk")


@pytest.mark.parametrize("source,target", tuple(permutations(("aws", "gcp", "azure"), 2)))
def test_common_order_handles_all_six_routes(source, target):
    inp = plan(source, target)
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    assert [role for role, _ in tf.applies] == ["source", "target", "oci", "source", "target"]
    assert flow.destroy_vpn(RUN_ID, terraform=tf)


@pytest.mark.parametrize("source,target,azure_role", [("aws", "azure", "target"), ("azure", "gcp", "source")])
def test_azure_gateway_is_built_first_and_connected_after_oci(source, target, azure_role):
    inp = plan(source, target)
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    first, second = [values for role, values in tf.applies if role == azure_role]
    endpoint = getattr(inp, azure_role)
    psk = resolve(f"secret://{RUN_ID}/vpn-psk")

    assert first["oci_tunnels"] == [] and first["shared_secrets"] == []
    assert first["gateway_bgp_ips"] == [endpoint.peer_bgp_ip(0), endpoint.peer_bgp_ip(1)]
    assert first["resource_group"] == "rg" and first["vnet_name"] == "net" and first["location"] == "region"
    assert second["oci_tunnels"] == [
        {"oci_ip": OCI_IPS[key], "oci_bgp_ip": endpoint.oracle_bgp_ip(i)} for i, key in enumerate(endpoint.keys)]
    assert second["shared_secrets"] == [psk[key] for key in endpoint.keys]


def test_only_azure_tunnels_change_the_oci_pfs_group():
    inp = plan("aws", "azure")
    tf = FakeTerraform(inp)
    flow.ensure_vpn(inp, terraform=tf)
    tunnels = next(values for role, values in tf.applies if role == "oci")["tunnels"]

    assert tunnels["target-0"]["phase_two_dh_group"] == tunnels["target-1"]["phase_two_dh_group"] == "GROUP14"
    assert tunnels["target-0"]["peer_asn"] == "65515"
    assert "phase_two_dh_group" not in tunnels["source-0"] and "phase_two_dh_group" not in tunnels["source-1"]


@pytest.mark.parametrize("change", [{"has_gateway_subnet": False}, {"resource_group": None}, {"vnet_name": None}])
def test_azure_without_required_network_is_rejected_before_any_resource(change):
    inp = plan("aws", "azure")
    inp = replace(inp, target=replace(inp.target, network={**inp.target.network, **change}))
    tf = FakeTerraform(inp)
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "invalid_plan" and not tf.applies


@pytest.mark.parametrize("name,value", [("gateway_asn", 64512), ("gateway_bgp_ips", ["169.254.31.1", "169.254.32.1"])])
def test_azure_unexpected_bgp_output_stops_before_oci(name, value):
    inp = plan("aws", "azure")
    tf = FakeTerraform(inp)
    tf.apply("target", {})
    tf.applies.clear()
    tf.state["target"][name] = value
    with pytest.raises(VpnError) as exc:
        ADAPTERS["azure"].peers(tf, inp.target)
    assert exc.value.code == "invalid_output"


def test_azure_credentials_become_terraform_environment():
    assert ADAPTERS["azure"].credentials(resolve("secret://cloud/azure")) == {
        "ARM_TENANT_ID": "tenant", "ARM_CLIENT_ID": "client", "ARM_CLIENT_SECRET": SECRET, "ARM_SUBSCRIPTION_ID": "sub"}
    with pytest.raises(KeyError):
        ADAPTERS["azure"].credentials({"tenant_id": "tenant"})


def test_context_contains_topology_and_credential_references_only():
    inp = plan()
    flow.ensure_vpn(inp, terraform=FakeTerraform(inp))
    stored = resolve(f"secret://{RUN_ID}/vpn-context")["json"]
    assert "secret://cloud/aws" in stored and SECRET not in stored
    assert VpnPlan.from_context(json.loads(stored)) == inp


def test_from_route_checks_injected_identity():
    from app.features.db_migration.cloud_lookup import FoundDb, FoundEndpoint, FoundRoute
    from app.features.db_migration.run_input import InjectedEndpoint, InjectedRun

    inp = plan()
    def endpoint(side):
        return FoundEndpoint(side.cloud, side.region, side.scope_id, FoundDb("db", "host", 6543, "16", False, True),
                             side.cidrs, side.network)
    injected = InjectedRun(RUN_ID, InjectedEndpoint("aws", "region", "secret://cloud/aws", "secret://db"),
                           InjectedEndpoint("gcp", "region", "secret://cloud/gcp", "secret://db"), "alpha")
    found = FoundRoute(endpoint(inp.source), endpoint(inp.target))
    assert VpnPlan.from_route(injected, found) == inp
    with pytest.raises(VpnError, match="does not match"):
        VpnPlan.from_route(injected, replace(found, source=replace(found.source, cloud="gcp")))


def test_same_run_cannot_interleave_ensure_and_destroy():
    inp = plan()
    with flow._operation(RUN_ID):
        with pytest.raises(VpnError) as exc:
            flow.ensure_vpn(inp, terraform=FakeTerraform(inp))
        assert exc.value.code == "busy"


def test_legacy_context_is_not_overwritten():
    put_secret(f"{RUN_ID}/vpn-context", {"json": json.dumps({"run_id": RUN_ID, "source": {}})})
    inp = plan()
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=FakeTerraform(inp))
    assert exc.value.code == "legacy_context"


def test_destroy_without_context_has_no_side_effect():
    assert flow.destroy_vpn("mig-unused") is False


def test_terraform_role_credentials_state_and_file_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ambient-gcp-file")
    monkeypatch.setenv("TF_LOG", "TRACE")
    calls = []
    paths = []
    def runner(command, env, **kwargs):
        calls.append((command, env))
        if command[2] == "apply":
            path = next(arg.removeprefix("-var-file=") for arg in command if arg.startswith("-var-file="))
            from pathlib import Path
            paths.append(Path(path))
            assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        return subprocess.CompletedProcess(command, 0, "{}", "")

    tf = TerraformSession(vpn.OciSettings.from_env(), JOB_ID, {"source": "gcp", "target": "aws-vpn", "oci": "oci-vpn"},
                           {"source": lambda: {"GOOGLE_CREDENTIALS": SECRET},
                            "target": lambda: {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": SECRET},
                            "oci": lambda: {}}, runner=runner)
    for role in ("source", "target", "oci"):
        tf.apply(role, {"shared_secrets": [SECRET]})
    assert all(not p.exists() for p in paths)
    for command, env in calls:
        assert SECRET not in " ".join(command)
        assert all(key not in env for key in ("ALPHA_SECRETS_KEY", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "TF_LOG"))
        role = next(role for role in ("source", "target", "oci") if f"/{role}/.terraform" in env["TF_DATA_DIR"])
        assert ("GOOGLE_CREDENTIALS" in env) is (role == "source")
        assert ("AWS_SECRET_ACCESS_KEY" in env) is (role == "target")
        if command[2] == "init":
            assert f"-backend-config=key={JOB_ID}/vpn-v2/{role}.tfstate" in command


@pytest.mark.parametrize("failure", ["exit", "exception", "json"])
def test_terraform_failures_never_quote_stderr_or_leave_tfvars(tmp_path, failure):
    def runner(command, **kwargs):
        if command[2] == "init":
            return subprocess.CompletedProcess(command, 0, "", "")
        if failure == "exception":
            raise subprocess.TimeoutExpired(command, 1, stderr=SECRET)
        return subprocess.CompletedProcess(command, 0 if failure == "json" else 1, SECRET, SECRET)
    tf = TerraformSession(vpn.OciSettings.from_env(), JOB_ID, {"source": "gcp"}, {"source": lambda: {}}, runner=runner)
    with pytest.raises(VpnError) as exc:
        tf.output("source", "x") if failure == "json" else tf.apply("source", {"shared_secret": SECRET})
    assert SECRET not in str(exc.value)
    assert not list(tmp_path.rglob("*.tfvars.json"))


def test_bad_network_fails_before_building_any_cloud():
    inp = plan()
    inp = replace(inp, source=replace(inp.source, network={}))
    tf = FakeTerraform(inp)
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "invalid_plan" and not tf.applies


def test_missing_credentials_is_found_before_context_is_saved():
    delete_secret("secret://cloud/gcp")
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(plan())
    assert exc.value.code == "credentials"
    assert not has_secret(f"secret://{RUN_ID}/vpn-context")


@pytest.mark.parametrize("wrong", [{"source-0": "9.0.0.1"}, {key: "not-an-ip" for key in TUNNEL_KEYS}])
def test_invalid_oci_outputs_never_start_peer_connection(wrong):
    inp = plan()
    tf = FakeTerraform(inp)
    apply = tf.apply
    def broken(role, values):
        apply(role, values)
        if role == "oci":
            tf.state[role]["oracle_tunnel_ips"] = wrong
    tf.apply = broken
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "invalid_output"
    assert [role for role, _ in tf.applies] == ["source", "target", "oci"]


def test_one_or_extra_up_tunnel_cannot_pass_health_check(monkeypatch):
    monkeypatch.setenv("CDC_TUNNEL_WAIT_S", "-1")
    inp = plan()
    tf = FakeTerraform(inp)
    apply = tf.apply
    def broken(role, values):
        apply(role, values)
        if role == "oci":
            tf.state[role]["tunnel_status"] = {"source-0": "UP"}
    tf.apply = broken
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert exc.value.code == "tunnel_timeout"


def test_temporary_aws_token_is_passed_by_adapter_only():
    from app.features.db_migration.vpn_peers import AwsPeer

    env = AwsPeer().credentials({"aws_access_key_id": "key", "aws_secret_access_key": "secret", "aws_session_token": "token"})
    assert env == {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "token"}


def test_job_mismatch_cannot_use_another_runs_state():
    inp = plan()
    with pytest.raises(VpnError, match="does not match"):
        vpn.ensure_vpn(inp, "another_job", terraform=FakeTerraform(inp))
    assert not has_secret(f"secret://{RUN_ID}/vpn-context")


@pytest.mark.parametrize("changed", [
    {"job_id": "../../escape"},
    {"run_id": "../escape"},
    {"target": leg("target", "aws")},
    {"source": replace(leg("source", "aws"), cidrs=(SECRET,))},
    {"source": replace(leg("source", "aws"), db_port=0)},
])
def test_invalid_plan_cannot_write_context(changed):
    inp = replace(plan(), **changed)
    tf = FakeTerraform(inp)
    with pytest.raises(VpnError) as exc:
        flow.ensure_vpn(inp, terraform=tf)
    assert SECRET not in str(exc.value) and not tf.applies


def test_new_context_cannot_be_loaded_as_legacy_input():
    inp = plan()
    flow.ensure_vpn(inp, terraform=FakeTerraform(inp))
    with pytest.raises(VpnError, match="legacy input must not overwrite"):
        vpn.VpnContext.load(RUN_ID)


def test_malformed_credential_values_fail_before_terraform_is_invoked():
    invoked = []
    tf = TerraformSession(vpn.OciSettings.from_env(), JOB_ID, {"source": "gcp"},
                           {"source": lambda: {"GOOGLE_CREDENTIALS": {"bad": SECRET}}},
                           runner=lambda *args, **kwargs: invoked.append(args))
    with pytest.raises(VpnError) as exc:
        tf.check_credentials()
    assert exc.value.code == "credentials" and SECRET not in str(exc.value) and not invoked
