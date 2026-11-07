"""실제 API 응답 형태를 흉내 내어 조회 범위·페이지 처리·실패 경계를 확인한다."""

import copy
import json
from itertools import permutations
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.secrets import put_secret
from app.features.db_migration import cloud_lookup as lookup
from app.features.db_migration.run_input import inject, parse
from app.features.db_migration.start_checks import StartCheckInput

REGIONS = {"aws": "ap-northeast-2", "gcp": "asia-northeast3", "azure": "koreacentral"}
SECRET = "do-not-echo-this-secret"
CREDS = {
    "aws": {"aws_access_key_id": "test-key", "aws_secret_access_key": SECRET},
    "gcp": {"credentials_json": json.dumps({"project_id": "db-project", "private_key": SECRET})},
    "azure": {"tenant_id": "tenant", "client_id": "client", "client_secret": SECRET, "subscription_id": "sub"},
}
ROOT = "/subscriptions/sub/resourceGroups/rg/providers"
SERVER = f"{ROOT}/Microsoft.DBforPostgreSQL/flexibleServers/db"
VNET = f"{ROOT}/Microsoft.Network/virtualNetworks/net"
PE = f"{ROOT}/Microsoft.Network/privateEndpoints/pe"
NIC = f"{ROOT}/Microsoft.Network/networkInterfaces/nic"
GCP_NET = "projects/host-project/global/networks/net"


def request_input(source="aws", target="gcp", run_id="mig-lookup"):
    return {
        "run_id": run_id,
        "source": {"cloud": source, "region": REGIONS[source], "db": {"user": "alpha", "password": SECRET}},
        "target": {"cloud": target, "region": REGIONS[target],
                   "db": {"user": "admin", "password": SECRET, "app_user": "alpha"}},
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    for cloud, values in CREDS.items():
        put_secret(f"cloud/{cloud}", values)
    data = {
        "aws": [{"DBInstanceIdentifier": "db", "Engine": "postgres", "DBInstanceStatus": "available",
                 "Endpoint": {"Address": "db.rds.example", "Port": 5432}, "EngineVersion": "16.4",
                 "PubliclyAccessible": False,
                 "DBSubnetGroup": {"VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-a"}, {"SubnetIdentifier": "subnet-b"}]},
                 "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1", "Status": "active"}],
                 "DBParameterGroups": [{"DBParameterGroupName": "params", "ParameterApplyStatus": "in-sync"}]}],
        "vpc": {"CidrBlock": "172.31.0.0/16", "CidrBlockAssociationSet": [
            {"CidrBlock": "172.31.0.0/16", "CidrBlockState": {"State": "associated"}},
            {"CidrBlock": "172.30.0.0/16", "CidrBlockState": {"State": "associated"}}]},
        "routes": [
            {"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
            {"RouteTableId": "rtb-a", "Associations": [{"SubnetId": "subnet-a"}]}],
        "gcp": [{"name": "db", "region": REGIONS["gcp"], "databaseVersion": "POSTGRES_16",
                 "instanceType": "CLOUD_SQL_INSTANCE", "state": "RUNNABLE",
                 "ipAddresses": [{"type": "PRIMARY", "ipAddress": "34.1.1.1"}, {"type": "PRIVATE", "ipAddress": "10.20.0.5"}],
                 "settings": {"ipConfiguration": {"privateNetwork": GCP_NET, "ipv4Enabled": False},
                              "databaseFlags": [{"name": "cloudsql.logical_decoding", "value": "on"}]}}],
        "azure": [{"id": SERVER, "name": "db", "location": "Korea Central", "properties": {"replica": {"role": "Primary"}}}],
        "server": {"id": SERVER, "name": "db", "properties": {
            "version": "16", "state": "Ready", "network": {"publicNetworkAccess": "Disabled"},
            "privateEndpointConnections": [{"properties": {"privateEndpoint": {"id": PE},
                "privateLinkServiceConnectionState": {"status": "Approved"}}}]}},
        "pe": {"properties": {"subnet": {"id": f"{VNET}/subnets/private"}, "networkInterfaces": [{"id": NIC}]}},
        "nic": {"properties": {"ipConfigurations": [{"properties": {"privateIPAddress": "10.30.1.5"}}]}},
        "net": {"name": "net", "properties": {"addressSpace": {"addressPrefixes": ["10.30.0.0/16"]},
                                                  "subnets": [{"name": "GatewaySubnet"}]}},
        "wal": {"properties": {"value": "logical", "isConfigPendingRestart": False}},
        "requests": [], "errors": {}, "clients": [],
    }

    def aws_client(service, region, credentials):
        assert region == REGIONS["aws"] and credentials == CREDS["aws"]
        data["clients"].append(service)

        def paginator(name):
            def paginate(**kwargs):
                if name == "describe_db_instances":
                    return [{"DBInstances": [{"Engine": "mysql"}]}, {"DBInstances": data["aws"]}]
                if name == "describe_route_tables":
                    assert kwargs == {"Filters": [{"Name": "vpc-id", "Values": ["vpc-1"]}]}
                    return [{"RouteTables": data["routes"][:1]}, {"RouteTables": data["routes"][1:]}]
                assert name == "describe_db_parameters" and kwargs == {"DBParameterGroupName": "params"}
                return [{"Parameters": []}, {"Parameters": [{"ParameterName": "rds.logical_replication", "ParameterValue": "1"}]}]
            return SimpleNamespace(paginate=paginate)

        return SimpleNamespace(
            get_paginator=paginator,
            describe_vpcs=lambda **kwargs: {"Vpcs": [data["vpc"]]},
            describe_route_tables=lambda **kwargs: {"RouteTables": data["routes"]},
            get_caller_identity=lambda: {"Account": "account"},
        )

    def handler(request):
        data["requests"].append(request)
        path = request.url.path
        if path in data["errors"]:
            return httpx.Response(data["errors"][path], json={"error": {"code": SECRET, "message": SECRET}})
        if request.url.host == "login.microsoftonline.com":
            assert request.method == "POST" and b"client_secret=" in request.content
            return httpx.Response(200, json={"access_token": "azure-token"})
        assert request.method == "GET"
        assert request.headers["Authorization"] in ("Bearer gcp-token", "Bearer azure-token")
        if path == "/v1/projects/db-project/instances":
            if request.url.params.get("pageToken"):
                body = {"items": data["gcp"]}
            else:
                body = {"items": [{"region": "other", "databaseVersion": "POSTGRES_16"}], "nextPageToken": "sql-page-2"}
        elif path == "/compute/v1/projects/host-project/aggregated/subnetworks":
            if request.url.params.get("pageToken"):
                body = {"items": {"regions/other": {"subnetworks": [
                    {"network": GCP_NET, "ipCidrRange": "10.12.0.0/24", "secondaryIpRanges": [{"ipCidrRange": "10.13.0.0/24"}]}]}}}
            else:
                body = {"items": {"regions/local": {"subnetworks": [
                    {"network": GCP_NET, "ipCidrRange": "10.11.0.0/24"},
                    {"network": "projects/other-project/global/networks/net", "ipCidrRange": "172.31.0.0/16"}]}},
                        "nextPageToken": "subnet-page-2"}
        elif path == "/compute/v1/projects/host-project/global/addresses":
            body = {"items": [
                {"network": GCP_NET, "purpose": "VPC_PEERING", "address": "10.20.0.0", "prefixLength": 20},
                {"network": GCP_NET, "purpose": "VPC_PEERING", "address": "10.21.0.0", "prefixLength": 20}]}
        elif path == "/subscriptions/sub/providers/Microsoft.DBforPostgreSQL/flexibleServers":
            if request.url.params.get("page"):
                body = {"value": data["azure"]}
            else:
                body = {"value": [], "nextLink": f"{lookup.AZURE_ARM}{path}?page=2&api-version=2024-08-01"}
        else:
            body = {SERVER: data["server"], PE: data["pe"], NIC: data["nic"], VNET: data["net"],
                    f"{SERVER}/configurations/wal_level": data["wal"]}.get(path)
            assert body is not None, str(request.url)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(lookup, "_aws_client", aws_client)
    monkeypatch.setattr(lookup, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(lookup, "_gcp_token", lambda key: "gcp-token")
    return data


@pytest.mark.parametrize("source,target", tuple(permutations(REGIONS, 2)))
def test_lookup_all_six_routes_from_injected_credentials(world, source, target):
    found = lookup.find_route(inject("mig-lookup", parse(request_input(source, target))))
    assert (found.source.cloud, found.target.cloud) == (source, target)
    assert (found.source.region, found.target.region) == (REGIONS[source], REGIONS[target])
    assert found.source.db.port == found.target.db.port == 5432
    assert found.source.db.logical_replication is True
    assert SECRET not in repr(found)
    common = StartCheckInput.from_route(found)
    assert common.source_cidrs and common.target_cidrs and common.source_database is None


@pytest.mark.parametrize("cloud", REGIONS)
@pytest.mark.parametrize("count", [0, 2])
def test_zero_or_multiple_primary_instances_are_not_guessed(world, cloud, count):
    world[cloud] = world[cloud] * count
    with pytest.raises(lookup.LookupFailed, match="primary PostgreSQL"):
        lookup.find(cloud, REGIONS[cloud], f"secret://cloud/{cloud}")


def test_aws_routes_security_groups_and_extra_ranges(world):
    found = lookup.find("aws", REGIONS["aws"], "secret://cloud/aws")
    assert found.cidrs == ("172.31.0.0/16", "172.30.0.0/16")
    assert found.network["route_table_ids"] == ["rtb-a", "rtb-main"]
    assert found.network["db_security_group_ids"] == ["sg-1"]


def test_gcp_includes_other_regions_secondary_ranges_and_all_psa_ranges(world):
    found = lookup.find("gcp", REGIONS["gcp"], "secret://cloud/gcp")
    assert found.db.host == "10.20.0.5" and found.scope_id == "db-project"
    assert found.network["project_id"] == "host-project"
    assert found.cidrs == ("10.11.0.0/24", "10.12.0.0/24", "10.13.0.0/24", "10.20.0.0/20", "10.21.0.0/20")


def test_azure_private_endpoint_and_vnet(world):
    found = lookup.find("azure", REGIONS["azure"], "secret://cloud/azure")
    assert found.db.host == "10.30.1.5" and found.db.publicly_accessible is False
    assert found.network["pe_subnet_id"] == f"{VNET}/subnets/private"
    assert found.network["has_gateway_subnet"] is True


@pytest.mark.parametrize("cloud", REGIONS)
def test_read_replicas_are_not_selected(world, cloud):
    replica = copy.deepcopy(world[cloud][0])
    if cloud == "aws":
        replica["ReadReplicaSourceDBInstanceIdentifier"] = "primary"
    elif cloud == "gcp":
        replica["instanceType"] = "READ_REPLICA_INSTANCE"
    else:
        replica["properties"]["replica"]["role"] = "AsyncReplica"
    world[cloud].append(replica)
    lookup.find(cloud, REGIONS[cloud], f"secret://cloud/{cloud}")


@pytest.mark.parametrize("cloud", REGIONS)
def test_not_ready_instance_is_refused(world, cloud):
    if cloud == "aws":
        world[cloud][0]["DBInstanceStatus"] = "creating"
    elif cloud == "gcp":
        world[cloud][0]["state"] = "PENDING_CREATE"
    else:
        world["server"]["properties"]["state"] = "Starting"
    with pytest.raises(lookup.LookupFailed, match="not (available|runnable|ready)"):
        lookup.find(cloud, REGIONS[cloud], f"secret://cloud/{cloud}")


def test_aws_missing_one_route_is_not_partial_success(world):
    world["routes"] = [world["routes"][1]]
    with pytest.raises(lookup.LookupFailed, match="route table"):
        lookup.find("aws", REGIONS["aws"], "secret://cloud/aws")


def test_pending_logical_replication_is_not_reported_as_applied(world):
    world["aws"][0]["DBParameterGroups"][0]["ParameterApplyStatus"] = "pending-reboot"
    world["wal"]["properties"]["isConfigPendingRestart"] = True
    assert lookup.find("aws", REGIONS["aws"], "secret://cloud/aws").db.logical_replication is False
    assert lookup.find("azure", REGIONS["azure"], "secret://cloud/azure").db.logical_replication is False


def test_multiple_azure_endpoints_are_not_selected_arbitrarily(world):
    properties = world["server"]["properties"]
    another = copy.deepcopy(properties["privateEndpointConnections"][0])
    another["properties"]["privateEndpoint"]["id"] += "2"
    properties["privateEndpointConnections"].append(another)
    with pytest.raises(lookup.LookupFailed, match="multiple approved"):
        lookup.find("azure", REGIONS["azure"], "secret://cloud/azure")


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_api_errors_show_status_without_response_or_secret(world, status):
    world["errors"]["/v1/projects/db-project/instances"] = status
    with pytest.raises(lookup.LookupFailed, match=str(status)) as exc:
        lookup.find_route(inject("mig-lookup", parse(request_input("gcp", "aws"))))
    assert "source (gcp)" in str(exc.value) and SECRET not in str(exc.value)


def test_unexpected_exception_never_exposes_aws_response(world, monkeypatch):
    def fail(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(lookup, "_aws_client", fail)
    with pytest.raises(lookup.LookupFailed) as exc:
        lookup.find("aws", REGIONS["aws"], "secret://cloud/aws")
    assert SECRET not in str(exc.value) and exc.value.__suppress_context__


@pytest.mark.parametrize("body", [[], SECRET])
def test_non_object_response_is_not_success(body):
    with pytest.raises(lookup.LookupFailed, match="invalid response"):
        lookup._json_or_fail(httpx.Response(200, json=body), "Cloud SQL")


def test_pagination_cannot_forward_azure_token_to_another_host():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"value": [], "nextLink": "https://other.example/steal"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(lookup.LookupFailed, match="pagination"):
            list(lookup._azure_pages(client, f"{lookup.AZURE_ARM}/subscriptions/sub/list", "token", "Azure", "1"))
    assert len(seen) == 1


def test_repeated_gcp_page_token_is_not_an_infinite_loop():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nextPageToken": "same"}))) as client:
        with pytest.raises(lookup.LookupFailed, match="pagination"):
            list(lookup._gcp_pages(client, "https://sqladmin.googleapis.com/list", "token", "Cloud SQL"))


def test_missing_azure_public_access_stays_unknown(world):
    world["server"]["properties"]["network"].clear()
    assert lookup.find("azure", REGIONS["azure"], "secret://cloud/azure").db.publicly_accessible is None


def test_public_access_is_reported_for_step_six_to_decide(world):
    world["aws"][0]["PubliclyAccessible"] = True
    route = lookup.find_route(inject("mig-lookup", parse(request_input())))
    assert route.source.db.publicly_accessible is True


@pytest.mark.parametrize("cloud", REGIONS)
def test_missing_private_network_is_not_success(world, cloud):
    if cloud == "aws":
        world["aws"][0]["DBSubnetGroup"].clear()
    elif cloud == "gcp":
        world["gcp"][0]["settings"]["ipConfiguration"].pop("privateNetwork")
    else:
        world["server"]["properties"]["privateEndpointConnections"].clear()
    with pytest.raises(lookup.LookupFailed):
        lookup.find(cloud, REGIONS[cloud], f"secret://cloud/{cloud}")


def test_missing_credentials_is_sanitized(world):
    with pytest.raises(lookup.LookupFailed) as exc:
        lookup.find("aws", REGIONS["aws"], f"secret://{SECRET}")
    assert SECRET not in str(exc.value)


def test_gcp_psa_must_hold_the_selected_database_address(world):
    world["gcp"][0]["ipAddresses"][1]["ipAddress"] = "10.99.0.5"
    with pytest.raises(lookup.LookupFailed, match="no private service access range"):
        lookup.find("gcp", REGIONS["gcp"], "secret://cloud/gcp")


def test_partial_gcp_listing_cannot_choose_a_single_instance_from_partial_results():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "items": [{"name": "one"}], "warnings": [{"code": "REGION_UNREACHABLE"}]}))) as client:
        with pytest.raises(lookup.LookupFailed, match="incomplete listing"):
            list(lookup._gcp_pages(client, "https://sqladmin.googleapis.com/list", "token", "Cloud SQL"))


def test_unreachable_gcp_scope_is_not_an_empty_network(monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "items": {}, "unreachables": ["regions/other"]}))) as client:
        with pytest.raises(lookup.LookupFailed, match="incomplete listing"):
            lookup._gcp_subnets(client, "token", "host-project", "net")


def test_multiple_private_endpoint_ips_are_not_guessed(world):
    world["nic"]["properties"]["ipConfigurations"].append({"properties": {"privateIPAddress": "10.30.1.6"}})
    with pytest.raises(lookup.LookupFailed):
        lookup.find("azure", REGIONS["azure"], "secret://cloud/azure")


def test_ipv6_vpc_range_is_returned_for_overlap_checks(world):
    world["vpc"]["Ipv6CidrBlockAssociationSet"] = [{
        "Ipv6CidrBlock": "fd00::/56", "Ipv6CidrBlockState": {"State": "associated"}}]
    assert "fd00::/56" in lookup.find("aws", REGIONS["aws"], "secret://cloud/aws").cidrs


def test_lookup_output_runs_through_common_start_checks(world):
    from app.features.db_migration.start_checks import CdcNetwork, check_start

    route = lookup.find_route(inject("mig-lookup", parse(request_input())))
    agent = SimpleNamespace(status=lambda: {"kafka": True, "connect": True, "plugins_missing": []}, list_jobs=lambda: [])
    check_start(StartCheckInput.from_route(route), job_id="mig_lookup", agent=agent,
                network=CdcNetwork("10.50.0.0/16", "10.50.1.0/24"))
