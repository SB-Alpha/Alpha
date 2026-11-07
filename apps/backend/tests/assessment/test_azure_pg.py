from types import SimpleNamespace as N

import contextlib

import pytest

from app.features.assessment import azure_pg, db
from app.features.assessment.schemas import AssessmentInput

# 검증용 cloud_source(#94 입력). 중계 수집기는 읽지 않고 db_instance_id 등을 쓴다
KW = dict(aws_credentials_ref="x", aws_region="r", target_region="t", source_kind="azure",
          cloud_source=dict(region="koreacentral", app_name="a", db_instance="s", subscription_id="sub",
                            resource_group="rg", credentials_ref="secret://x"))


class Err(Exception):
    def __init__(self, status=None, code=None):
        self.status_code = status
        self.error = N(code=code) if code else None


def server(public="Enabled", subnet=None, location=None):
    return N(location=location, version="16", sku=N(name="Standard_D2s_v3", tier="GeneralPurpose"), storage=N(storage_size_gb=128),
             network=N(public_network_access=public, delegated_subnet_resource_id=subnet),
             fully_qualified_domain_name="s.postgres.database.azure.com")


def factory(srv=None, flag="logical", rules=(1,), get_err=None, cfg_err=None):
    def cfg(rg, n, k):
        assert k == "wal_level"
        if cfg_err:
            raise cfg_err
        return N(value=flag)

    def get(rg, n):
        if get_err:
            raise get_err
        return srv or server()

    c = N(servers=N(get=get), configurations=N(get=cfg), firewall_rules=N(list_by_server=lambda rg, n: list(rules)))
    return lambda session, sub: c


SUB = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network"


def compute(vms=(), running=("b1",)):
    # vms: (name, tags, nic 이름)
    items = [N(id=f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{n}", tags=t,
               network_profile=N(network_interfaces=[N(id=f"{SUB}/networkInterfaces/{nic}")])) for n, t, nic in vms]
    c = N(virtual_machines=N(list_all=lambda: items, instance_view=lambda rg, n: N(
        statuses=[N(code="PowerState/running" if n in running else "PowerState/deallocated")])))
    return lambda session, sub: c


def net(nics=None, pips=None):
    # nics: nic -> (vnet, private ip, public ip 이름 또는 None)
    nics, pips = nics or {}, pips or {}

    def nic(rg, n):
        v, ip, pip = nics[n]
        return N(ip_configurations=[N(private_ip_address=ip, subnet=N(id=f"{SUB}/virtualNetworks/{v}/subnets/x"),
                                      public_ip_address=N(id=f"{SUB}/publicIPAddresses/{pip}") if pip else None)])

    c = N(network_interfaces=N(get=nic), public_ip_addresses=N(get=lambda rg, n: N(ip_address=pips[n])),
          private_endpoints=N(list_by_subscription=lambda: []))
    return lambda session, sub: c


TAG = {"Purpose": "alpha-assessment-bastion"}
PRIV = lambda: server(public="Disabled", subnet=f"{SUB}/virtualNetworks/v1/subnets/db")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(azure_pg.shutil, "which", lambda n: None)
    monkeypatch.delenv("ALPHA_AZURE_BASTION", raising=False)
    monkeypatch.delenv(azure_pg.BASTION_VM_ENV, raising=False)
    monkeypatch.setenv("ALPHA_AZURE_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("ALPHA_ALLOW_ENV_REFS", "1")
    monkeypatch.setenv("AZ_USER", "u")
    monkeypatch.setenv("AZ_PASSWORD", "pw-secret")


def run(f, rid="rg/srv", ref="env://AZ", cf=None, nf=None):
    cf = cf or compute()
    nf = nf or net()
    return azure_pg.collect(AssessmentInput(db_instance_id=rid, db_admin_ref=ref, **KW), None, f, cf, nf)


def test_ok():
    r = run(factory())
    assert r.gaps == []
    assert (r.db.engine_version, r.db.instance_class, r.db.allocated_storage_gb) == ("16", "Standard_D2s_v3", 128)
    assert r.db.logical_replication is True and r.source_db.instance_id == "rg/srv"
    assert r.creds == {"user": "u", "password": "pw-secret"}
    with r.tunnel() as hp:
        assert hp == ("s.postgres.database.azure.com", 5432)


def test_region_is_cdc_format():
    # CDC(db_migration)의 Azure 리전 형식: 소문자·공백 없음. SDK 표시 이름도 같은 값이 되어야 한다.
    for raw in ("Korea Central", "koreacentral"):
        assert run(factory(server(location=raw))).region == "koreacentral"


def test_ha_backup_cidrs():
    srv = server()
    srv.high_availability, srv.backup = N(mode="ZoneRedundant"), N(backup_retention_days=9)
    rules = [N(start_ip_address="10.0.0.0", end_ip_address="10.0.0.255"), N(start_ip_address="1.1.1.1", end_ip_address="1.1.1.1"),
             N(start_ip_address="bad", end_ip_address="x"), 1]
    r = run(factory(srv, rules=rules))
    assert r.db.ha_enabled is True and r.db.backup_retention_days == 9
    assert r.network.allowed_cidrs == ["10.0.0.0/24", "1.1.1.1/32", "bad-x"]
    srv.high_availability = N(mode="Disabled")
    assert run(factory(srv)).db.ha_enabled is False


def test_ha_backup_absent():
    r = run(factory())
    assert r.db.ha_enabled is None and r.db.backup_retention_days is None and r.network.allowed_cidrs == []


def test_flag_off():
    assert run(factory(flag="replica")).db.logical_replication is False


def test_denied_on_config_keeps_none():
    r = run(factory(cfg_err=Err(403, "AuthorizationFailed")))
    assert r.db.logical_replication is None and any("AuthorizationFailed" in g for g in r.gaps)


def test_denied_on_server():
    r = run(factory(get_err=Err(403, "AuthorizationFailed")))
    assert r.source_db.instance_id is None and any("AuthorizationFailed" in g for g in r.gaps)


def test_not_found():
    r = run(factory(get_err=Err(404)))
    assert r.source_db.instance_id is None and any("찾지 못함" in g for g in r.gaps)


def test_no_subscription(monkeypatch):
    monkeypatch.delenv("ALPHA_AZURE_SUBSCRIPTION_ID")
    assert any("구독" in g for g in run(factory()).gaps)


@pytest.mark.parametrize("rid", ["srv", "a/b/c", "rg/Bad_Name"])
def test_bad_id(rid):
    r = run(factory(), rid=rid)
    assert any("형식" in g for g in r.gaps) and r.source_db.instance_id is None


def test_private_no_tunnel():
    r = run(factory(srv=PRIV()))
    assert r.tunnel is None and r.network.vpc_id == f"{SUB}/virtualNetworks/v1" and r.db.publicly_accessible is False


def test_no_firewall_rules_no_tunnel():
    assert run(factory(rules=())).tunnel is None


def test_no_creds_no_leak():
    r = run(factory(), ref=None)
    assert r.creds is None and r.tunnel is None and r.gaps


def test_integration_never_raises():
    out = db.collect(AssessmentInput(db_instance_id="rg/srv", **KW), None)
    assert out.gaps


def test_private_uses_bastion_public_ip(monkeypatch):
    calls = []
    monkeypatch.setattr(azure_pg.ssh_tunnel, "forward", lambda *a: calls.append(a) or "cm")
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1")]), nf=net({"n1": ("v1", "10.0.0.4", "p1")}, {"p1": "1.2.3.4"}))
    assert r.tunnel() == "cm" and calls == [("1.2.3.4", "s.postgres.database.azure.com", 5432)]


def test_bastion_private_ip_and_vnet_preferred(monkeypatch):
    calls = []
    monkeypatch.setattr(azure_pg.ssh_tunnel, "forward", lambda *a: calls.append(a))
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1"), ("b2", TAG, "n2")], running=("b1", "b2")),
            nf=net({"n1": ("other", "10.9.0.4", None), "n2": ("V1", "10.0.0.5", None)}))
    r.tunnel()
    assert calls[0][0] == "10.0.0.5"


@pytest.mark.parametrize("cf", [compute(), compute([("b1", {"Purpose": "x"}, "n1")]), compute([("b1", TAG, "n1")], running=())])
def test_private_no_bastion_gap(cf):
    r = run(factory(srv=PRIV()), cf=cf, nf=net({"n1": ("v1", "10.0.0.4", None)}))
    assert r.tunnel is None and any("bastion" in g for g in r.gaps)


def test_public_without_rules_falls_to_bastion(monkeypatch):
    monkeypatch.setenv(azure_pg.BASTION_VM_ENV, VM)
    r = run(factory(rules=()), cf=compute([("b1", TAG, "n1")]), nf=net({"n1": ("v1", "10.0.0.4", None)}))
    assert r.tunnel is not None and r.creds


def test_public_direct_preferred():
    r = run(factory(), cf=compute([("b1", TAG, "n1")]), nf=net({"n1": ("v1", "10.0.0.4", None)}))
    with r.tunnel() as hp:
        assert hp == ("s.postgres.database.azure.com", 5432)


VM = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/b1"


def bnet(nics, hosts):
    base = net(nics)(None, None)
    base.bastion_hosts = N(list=lambda: hosts)
    return lambda session, sub: base


def bhost(vnet, name="bh"):
    return N(id=f"{SUB.replace('Microsoft.Network', 'Microsoft.Network')}/bastionHosts/{name}",
             ip_configurations=[N(subnet=N(id=f"{SUB}/virtualNetworks/{vnet}/subnets/AzureBastionSubnet"))])


def test_find_bastion_returns_vm_id():
    sn = f"{SUB}/virtualNetworks/v1/subnets/db"
    r = azure_pg.find_bastion(None, "s", sn, compute([("b1", TAG, "n1")]), net({"n1": ("v1", "10.0.0.4", None)}))
    assert r[:2] == ("10.0.0.4", VM)


def test_other_vnet_rejected():
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1")]), nf=net({"n1": ("other", "10.9.0.4", None)}))
    assert r.tunnel is None and any("같은 VNet" in g for g in r.gaps)


def test_unknown_vnet_needs_pin(monkeypatch):
    cf, nf = compute([("b1", TAG, "n1")]), net({"n1": ("v1", "10.0.0.4", None)})
    assert azure_pg.find_bastion(None, "s", None, cf, nf)[0] is None
    monkeypatch.setenv(azure_pg.BASTION_VM_ENV, VM)
    assert azure_pg.find_bastion(None, "s", None, cf, nf)[:2] == ("10.0.0.4", VM)
    monkeypatch.setenv(azure_pg.BASTION_VM_ENV, VM.replace("b1", "b2"))
    assert azure_pg.find_bastion(None, "s", None, cf, nf)[0] is None
    monkeypatch.setenv(azure_pg.BASTION_VM_ENV, "bad;x")
    assert "형식" in azure_pg.find_bastion(None, "s", None, cf, nf)[3]


def test_azure_bastion_env_first(monkeypatch):
    monkeypatch.setenv("ALPHA_AZURE_BASTION", "rg2/mybast")
    assert azure_pg.find_azure_bastion(None, "s", "x", bnet({}, []))[0] == ("rg2", "mybast")
    monkeypatch.setenv("ALPHA_AZURE_BASTION", "bad;rm")
    assert azure_pg.find_azure_bastion(None, "s", "x", bnet({}, []))[0] is None


def test_azure_bastion_same_vnet():
    v = f"{SUB}/virtualNetworks/v1".lower()
    got = azure_pg.find_azure_bastion(None, "s", v, bnet({}, [bhost("other", "a"), bhost("V1", "b")]))
    assert got[0] == ("rg", "b")
    assert azure_pg.find_azure_bastion(None, "s", v, bnet({}, [bhost("other")]))[0] is None


@pytest.fixture
def procs(monkeypatch):
    ev = []

    class P:
        def poll(self):
            return None

    monkeypatch.setattr(azure_pg.shutil, "which", lambda n: "/bin/az")
    monkeypatch.setattr(azure_pg, "_free_port", lambda: 5555)
    monkeypatch.setattr(azure_pg, "_listening", lambda p: True)
    monkeypatch.setattr(azure_pg, "_stop", lambda p: ev.append("stop-az"))
    monkeypatch.setattr(azure_pg.subprocess, "Popen", lambda cmd, **kw: ev.append(cmd) or P())

    @contextlib.contextmanager
    def fwd(*a, **kw):
        ev.append(("ssh", a, kw))
        yield "127.0.0.1", 6666
        ev.append("stop-ssh")

    monkeypatch.setattr(azure_pg.ssh_tunnel, "forward", fwd)
    return ev


def test_bastion_tunnel_two_stage_cleanup(procs):
    with azure_pg.bastion_tunnel("rg", "bh", VM, "s.pg") as hp:
        assert hp == ("127.0.0.1", 6666)
    assert procs[0] == ["az", "network", "bastion", "tunnel", "--name", "bh", "--resource-group", "rg",
                        "--target-resource-id", VM, "--resource-port", "22", "--port", "5555"]
    assert procs[1] == ("ssh", ("127.0.0.1", "s.pg", 5432), {"ssh_port": 5555, "proxy_command": ""})
    assert procs[2:] == ["stop-ssh", "stop-az"]


def test_bastion_tunnel_stops_az_on_error(procs):
    with pytest.raises(RuntimeError):
        with azure_pg.bastion_tunnel("rg", "bh", VM, "s.pg"):
            raise RuntimeError
    assert procs[-1] == "stop-az"


@pytest.mark.parametrize("rg,name,vm", [("rg;x", "bh", VM), ("rg", "b h", VM), ("rg", "bh", VM + " --x"), ("rg", "bh", "vm")])
def test_bastion_tunnel_rejects_injection(procs, rg, name, vm):
    with pytest.raises(azure_pg.BastionTunnelError):
        with azure_pg.bastion_tunnel(rg, name, vm, "s.pg"):
            pass
    assert procs == []


def test_bastion_tunnel_no_az(procs, monkeypatch):
    monkeypatch.setattr(azure_pg.shutil, "which", lambda n: None)
    with pytest.raises(azure_pg.BastionTunnelError, match="az 없음"):
        with azure_pg.bastion_tunnel("rg", "bh", VM, "s.pg"):
            pass


def test_select_bastion_tunnel(procs):
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1")]),
            nf=bnet({"n1": ("v1", "10.0.0.4", None)}, [bhost("v1")]))
    with r.tunnel():
        pass
    assert procs[0][:4] == ["az", "network", "bastion", "tunnel"] and not r.gaps


def test_select_no_az_gap_and_ssh_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(azure_pg.ssh_tunnel, "forward", lambda *a: calls.append(a))
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1")]), nf=net({"n1": ("v1", "10.0.0.4", "p1")}, {"p1": "1.2.3.4"}))
    r.tunnel()
    assert calls[0][0] == "1.2.3.4" and any("az 없음" in g for g in r.gaps)


def test_select_az_but_no_bastion_resource_falls_back(monkeypatch):
    calls = []
    monkeypatch.setattr(azure_pg.shutil, "which", lambda n: "/bin/az")
    monkeypatch.setattr(azure_pg.ssh_tunnel, "forward", lambda *a: calls.append(a))
    r = run(factory(srv=PRIV()), cf=compute([("b1", TAG, "n1")]), nf=bnet({"n1": ("v1", "10.0.0.4", None)}, []))
    r.tunnel()
    assert calls[0][0] == "10.0.0.4" and any("Bastion" in g for g in r.gaps)


def pe_net(sub="sub", rg="rg", name="srv", nsg_rules=None):
    """서버를 가리키는 Private Endpoint 하나가 있는 가짜 네트워크."""
    srv = f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.DBforPostgreSQL/flexibleServers/{name}"
    pe = N(private_link_service_connections=[N(private_link_service_id=srv.upper())], manual_private_link_service_connections=None,
           subnet=N(id=f"{SUB}/virtualNetworks/v1/subnets/pe"))
    other = N(private_link_service_connections=[N(private_link_service_id="/x/other")], manual_private_link_service_connections=None,
              subnet=N(id=f"{SUB}/virtualNetworks/v9/subnets/pe"))
    rules = nsg_rules if nsg_rules is not None else [
        N(priority=100, direction="Inbound", access="Allow", protocol="Tcp", source_address_prefix="10.20.0.0/24",
          source_address_prefixes=None, destination_port_range="5432", destination_port_ranges=None),
        N(priority=110, direction="Inbound", access="Allow", protocol="Tcp", source_address_prefix=None,
          source_address_prefixes=["10.50.0.0/16", "VirtualNetwork"], destination_port_range=None, destination_port_ranges=["5000-5500"]),
        N(priority=120, direction="Inbound", access="Allow", protocol="Tcp", source_address_prefix="10.99.0.0/24",
          source_address_prefixes=None, destination_port_range="22", destination_port_ranges=None),
        N(priority=4000, direction="Inbound", access="Deny", protocol="*", source_address_prefix="*",
          source_address_prefixes=None, destination_port_range="*", destination_port_ranges=None)]
    base = net()(None, None)
    base.private_endpoints = N(list_by_subscription=lambda: [other, pe])
    base.subnets = N(get=lambda rg, v, sn: N(network_security_group=N(id=f"{SUB}/networkSecurityGroups/nsg-pe"), route_table=None))
    base.virtual_networks = N(get=lambda rg, v: N(address_space=N(address_prefixes=["10.20.0.0/16"])))
    base.network_security_groups = N(get=lambda rg, n: N(security_rules=rules))
    return lambda session, sub: base


def test_private_endpoint_fills_network():
    r = run(factory(srv=server(public="Disabled")), nf=pe_net())
    assert r.network.vpc_id == f"{SUB}/virtualNetworks/v1" and r.network.cidr == "10.20.0.0/16"
    assert r.network.db_security_group_id.endswith("/networkSecurityGroups/nsg-pe")
    assert r.network.allowed_cidrs == ["10.20.0.0/24", "10.50.0.0/16"]  # 5432 에 닿는 CIDR 만, 서비스 태그·다른 포트·deny 제외


def test_private_endpoint_none_leaves_network_empty():
    r = run(factory(srv=server(public="Disabled")))
    assert r.network.vpc_id is None and r.network.allowed_cidrs == [] and not any("Private Endpoint" in g for g in r.gaps)


def test_private_endpoint_lookup_error_is_gap():
    def boom(session, sub):
        raise Err(403, "AuthorizationFailed")

    r = run(factory(srv=server(public="Disabled")), nf=boom)
    assert any("Private Endpoint 조회 실패" in g for g in r.gaps) and r.network.vpc_id is None


def test_private_endpoint_vnet_selects_bastion_without_pin():
    nf = pe_net()(None, None)
    nf.network_interfaces = net({"n1": ("v1", "10.0.0.4", None)})(None, None).network_interfaces
    r = run(factory(srv=server(public="Disabled")), cf=compute([("b1", TAG, "n1")]), nf=lambda s, sub: nf)
    assert r.tunnel is not None and not any("VNet 을 알 수 없어" in g for g in r.gaps)


def test_size_from_capabilities():
    caps = [N(supported_flexible_server_editions=[N(supported_server_versions=[N(supported_vcores=[
        N(name="Standard_D2s_v3", v_cores=2, supported_memory_per_vcore_mb=4096)])])])]
    c = factory()(None, None)
    c.location_based_capabilities = N(execute=lambda loc: caps)
    r = run(lambda s, sub: c)
    assert (r.db.vcpu, r.db.memory_gib) == (2, 8.0)


def test_size_fallback_from_sku_name():
    def boom(loc):
        raise Err(403)
    c = factory()(None, None)
    c.location_based_capabilities = N(execute=boom)
    r = run(lambda s, sub: c)
    assert (r.db.vcpu, r.db.memory_gib) == (2, None) and r.gaps == []
