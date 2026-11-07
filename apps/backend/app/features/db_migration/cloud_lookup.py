"""클라우드 API로 DB와 네트워크를 찾아 같은 모양으로 돌려준다.

여기에는 값 조회만 있다. VPN, DB 접속, 사용자 DB와 테이블 조회는 하지 않는다 (VPN 뒤에 에이전트가 한다).
오류 문구에는 API 응답 원문을 넣지 않고 HTTP 상태와 고정된 안내만 남긴다.
권한 범위·리전에서 primary PostgreSQL이 정확히 하나여야 한다.
GCP는 PSA, Azure는 Private Endpoint를 지원한다. 네트워크 설정은 변경하지 않는다.
"""

import ipaddress
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.core.secrets import resolve

from .run_input import InjectedEndpoint, InjectedRun, REGION_PATTERNS

POSTGRES_PORT = 5432
HTTP_TIMEOUT_S = 30.0
GCP_NETWORK = re.compile(r"projects/([^/]+)/global/networks/([^/]+)$")
AZURE_RESOURCE_GROUP = re.compile(r"/resourceGroups/([^/]+)", re.IGNORECASE)
AZURE_ARM = "https://management.azure.com"
AZURE_POSTGRES_API = "2024-08-01"
AZURE_NETWORK_API = "2023-11-01"
GCP_SQL = "https://sqladmin.googleapis.com/v1"
GCP_COMPUTE = "https://compute.googleapis.com/compute/v1"
GCP_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class LookupFailed(RuntimeError):
    """사람이 고칠 수 있는 이유만 담는다. 키, 비밀번호, 응답 원문은 담지 않는다."""


@dataclass(frozen=True)
class FoundDb:
    instance: str
    host: str
    port: int
    version: str | None
    publicly_accessible: bool | None
    logical_replication: bool | None


@dataclass(frozen=True)
class FoundEndpoint:
    """cidrs는 대역 겹침 검사에 쓰는 공통 값이고, network는 그 클라우드의 VPN이 쓰는 값이다."""

    cloud: str
    region: str
    scope_id: str
    db: FoundDb
    cidrs: tuple[str, ...]
    network: dict[str, Any]


@dataclass(frozen=True)
class FoundRoute:
    source: FoundEndpoint
    target: FoundEndpoint


def network_exposure(db: FoundDb) -> str | None:
    """주소나 명시된 공인 접근 설정으로 알 수 있는 범위만 표시한다."""
    try:
        return "PRIVATE" if ipaddress.ip_address(db.host).is_private else "PUBLIC"
    except ValueError:
        return "PRIVATE" if db.publicly_accessible is False else None


def find_route(injected: InjectedRun) -> FoundRoute:
    """양쪽 조회만 한다. 공인 접근·대역 겹침 판정은 6번 시작 전 검사에서 한다."""
    return FoundRoute(source=_find_side("source", injected.source), target=_find_side("target", injected.target))


def find(cloud: str, region: str, credentials_ref: str) -> FoundEndpoint:
    try:
        if cloud not in _LOOKUPS or not REGION_PATTERNS[cloud].fullmatch(region):
            raise LookupFailed("unsupported cloud or invalid region")
        found = _LOOKUPS[cloud](region, resolve(credentials_ref))
        _validate(found)
        return found
    except LookupFailed:
        raise
    except Exception:  # noqa: BLE001 - 원문에는 키나 응답이 섞일 수 있다
        raise LookupFailed("lookup failed; check credentials, permissions and cloud API availability") from None


def _find_side(side: str, endpoint: InjectedEndpoint) -> FoundEndpoint:
    try:
        return find(endpoint.cloud, endpoint.region, endpoint.credentials_ref)
    except LookupFailed as exc:
        raise LookupFailed(f"{side} ({endpoint.cloud}): {exc}") from None


def _validate(found: FoundEndpoint) -> None:
    """불완전한 API 응답을 다음 단계의 정상 입력으로 넘기지 않는다."""
    if not all(isinstance(v, str) and v and v == v.strip() and "\n" not in v and "\r" not in v
               for v in (found.scope_id, found.db.instance, found.db.host)):
        raise LookupFailed("the DB instance has incomplete connection information")
    if type(found.db.port) is not int or not 1 <= found.db.port <= 65535:
        raise LookupFailed("the DB instance has an invalid port")
    if not found.cidrs:
        raise LookupFailed("the DB network has no address ranges")
    try:
        for cidr in found.cidrs:
            if not isinstance(cidr, str):
                raise ValueError
            ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        raise LookupFailed("the DB network has an invalid address range") from None


def _only_one(instances: list[dict], region: str) -> dict:
    if not instances:
        raise LookupFailed("no primary PostgreSQL instance in the selected region")
    if len(instances) > 1:
        raise LookupFailed("multiple primary PostgreSQL instances in the selected region; exactly one is required")
    return instances[0]


# ---- HTTP ----


def _http() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_S)


def _json_or_fail(response: httpx.Response, what: str) -> dict:
    if response.is_success:
        try:
            result = response.json()
        except ValueError:
            raise LookupFailed(f"{what} returned invalid JSON") from None
        if not isinstance(result, dict):
            raise LookupFailed(f"{what} returned an invalid response")
        return result
    # 오류 코드도 API가 반환한 문자열이다. 값으로 위장된 비밀값을 인용하지 않는다.
    raise LookupFailed(f"{what} returned HTTP {response.status_code}")


def _get(client: httpx.Client, url: str, token: str, what: str, params: dict[str, str] | None = None) -> dict:
    return _json_or_fail(client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params), what)


# ---- AWS ----


def _aws_client(service: str, region: str, credentials: dict[str, str]) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=credentials["aws_access_key_id"],
        aws_secret_access_key=credentials["aws_secret_access_key"],
        config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3}),
    )


def _find_aws(region: str, credentials: dict[str, str]) -> FoundEndpoint:
    rds = _aws_client("rds", region, credentials)
    instances = [
        db
        for page in rds.get_paginator("describe_db_instances").paginate()
        for db in page["DBInstances"]
        if db.get("Engine") == "postgres" and not db.get("ReadReplicaSourceDBInstanceIdentifier")
    ]
    db = _only_one(instances, region)
    if db.get("DBInstanceStatus") != "available":
        raise LookupFailed("the RDS instance is not available")
    endpoint = db.get("Endpoint") or {}
    if not endpoint.get("Address"):
        raise LookupFailed("the RDS instance has no address yet")

    subnet_group = db.get("DBSubnetGroup") or {}
    vpc_id = subnet_group.get("VpcId")
    if not vpc_id:
        raise LookupFailed("the RDS instance has no VPC")
    ec2 = _aws_client("ec2", region, credentials)
    vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    groups = db.get("VpcSecurityGroups") or []
    security_groups = [g["VpcSecurityGroupId"] for g in groups if g.get("Status") == "active"]
    if not security_groups:
        raise LookupFailed("the RDS instance has no active VPC security group")
    return FoundEndpoint(
        cloud="aws",
        region=region,
        scope_id=_aws_client("sts", region, credentials).get_caller_identity()["Account"],
        db=FoundDb(
            instance=db["DBInstanceIdentifier"],
            host=endpoint["Address"],
            port=endpoint.get("Port", POSTGRES_PORT),
            version=db.get("EngineVersion"),
            publicly_accessible=db.get("PubliclyAccessible"),
            logical_replication=_aws_logical_replication(rds, db),
        ),
        cidrs=_aws_cidrs(vpc),
        network={
            "vpc_id": vpc_id,
            "cidr": vpc["CidrBlock"],
            "route_table_ids": _aws_route_tables(ec2, vpc_id, subnet_group),
            "db_security_group_id": security_groups[0],
            "db_security_group_ids": security_groups,
        },
    )


def _aws_cidrs(vpc: dict) -> tuple[str, ...]:
    associated = [
        item["CidrBlock"]
        for item in vpc.get("CidrBlockAssociationSet", [])
        if item.get("CidrBlockState", {}).get("State") == "associated"
    ]
    ipv6 = [item["Ipv6CidrBlock"] for item in vpc.get("Ipv6CidrBlockAssociationSet", [])
            if item.get("Ipv6CidrBlockState", {}).get("State") == "associated"]
    return tuple([*(associated or [vpc["CidrBlock"]]), *ipv6])


def _aws_route_tables(ec2: Any, vpc_id: str, subnet_group: dict) -> list[str]:
    tables = [table for page in ec2.get_paginator("describe_route_tables").paginate(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]) for table in page["RouteTables"]]
    # 서브넷에 명시 연결이 없으면 VPC main 테이블을 쓴다.
    main = next((t["RouteTableId"] for t in tables for a in t.get("Associations", []) if a.get("Main")), None)
    explicit = {a["SubnetId"]: t["RouteTableId"] for t in tables for a in t.get("Associations", []) if a.get("SubnetId")}
    used = [explicit.get(s["SubnetIdentifier"], main) for s in subnet_group.get("Subnets", [])]
    route_tables = list(dict.fromkeys(table for table in used if table))
    if not used or any(table is None for table in used):
        raise LookupFailed("no route table for the subnets of the RDS instance")
    return route_tables


def _aws_logical_replication(rds: Any, db: dict) -> bool | None:
    groups = db.get("DBParameterGroups") or []
    if not groups:
        return None
    group = groups[0]
    for page in rds.get_paginator("describe_db_parameters").paginate(DBParameterGroupName=group["DBParameterGroupName"]):
        for parameter in page["Parameters"]:
            if parameter["ParameterName"] == "rds.logical_replication":
                # 재부팅 대기 중이면 아직 적용 전이다.
                return parameter.get("ParameterValue") == "1" and group.get("ParameterApplyStatus") == "in-sync"
    return False


# ---- GCP ----


def _gcp_token(key: dict) -> str:
    import google.auth.transport.urllib3
    import urllib3
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(key, scopes=list(GCP_SCOPES))
    credentials.refresh(google.auth.transport.urllib3.Request(urllib3.PoolManager()))
    return credentials.token


def check_credentials(cloud: str, credentials: dict[str, str]) -> None:
    """키를 쓰기 전에 형식만 본다. 권한이 맞는지는 실제 조회에서 드러난다."""
    if cloud == "gcp":
        _gcp_key(credentials)


def _gcp_key(credentials: dict[str, str]) -> dict:
    try:
        key = json.loads(credentials["credentials_json"])
    except ValueError:
        raise LookupFailed("credentials_json is not a service account key") from None
    if not isinstance(key, dict) or not key.get("project_id"):
        raise LookupFailed("credentials_json is not a service account key")
    return key


def _gcp_pages(client: httpx.Client, url: str, token: str, what: str, params: dict[str, str] | None = None) -> Iterator[dict]:
    query = dict(params or {})
    seen = set()
    while True:
        page = _get(client, url, token, what, query)
        if page.get("warnings"):
            raise LookupFailed(f"{what} returned an incomplete listing")
        yield from page.get("items", [])
        if not page.get("nextPageToken"):
            return
        next_token = page["nextPageToken"]
        if not isinstance(next_token, str) or next_token in seen:
            raise LookupFailed(f"{what} returned invalid pagination")
        seen.add(next_token)
        query["pageToken"] = next_token


def _find_gcp(region: str, credentials: dict[str, str]) -> FoundEndpoint:
    key = _gcp_key(credentials)
    project = quote(key["project_id"], safe="")
    token = _gcp_token(key)
    with _http() as client:
        instances = [
            db
            for db in _gcp_pages(client, f"{GCP_SQL}/projects/{project}/instances", token, "Cloud SQL")
            if db.get("region") == region
            and db.get("databaseVersion", "").startswith("POSTGRES")
            and db.get("instanceType", "CLOUD_SQL_INSTANCE") == "CLOUD_SQL_INSTANCE"
        ]
        db = _only_one(instances, region)
        if db.get("state") != "RUNNABLE":
            raise LookupFailed("the Cloud SQL instance is not runnable")
        ip_config = db.get("settings", {}).get("ipConfiguration", {})
        private_ip = next((a["ipAddress"] for a in db.get("ipAddresses", []) if a.get("type") == "PRIVATE"), None)
        network_link = GCP_NETWORK.search(ip_config.get("privateNetwork") or "")
        if not private_ip or not network_link:
            raise LookupFailed("the Cloud SQL instance has no private IP")
        network_project, vpc_name = network_link.groups()
        subnets = _gcp_subnets(client, token, network_project, vpc_name)
        psa_ranges = _gcp_psa_ranges(client, token, network_project, vpc_name, private_ip)
        psa_range = next(c for c in psa_ranges if ipaddress.ip_address(private_ip) in ipaddress.ip_network(c))

    flags = db.get("settings", {}).get("databaseFlags", [])
    return FoundEndpoint(
        cloud="gcp",
        region=region,
        scope_id=key["project_id"],
        db=FoundDb(
            instance=db["name"],
            host=private_ip,
            port=POSTGRES_PORT,
            version=db["databaseVersion"].removeprefix("POSTGRES_"),
            publicly_accessible=ip_config.get("ipv4Enabled"),
            logical_replication=any(f.get("name") == "cloudsql.logical_decoding" and f.get("value") == "on" for f in flags),
        ),
        cidrs=tuple(dict.fromkeys((*subnets, *psa_ranges))),
        network={
            "project_id": network_project,
            "vpc_name": vpc_name,
            "subnet_cidrs": subnets,
            "psa_range_cidr": psa_range,
            "psa_range_cidrs": psa_ranges,
        },
    )


def _gcp_network_is(link: str | None, project: str, vpc_name: str) -> bool:
    found = GCP_NETWORK.search(link or "")
    return bool(found) and found.groups() == (project, vpc_name)


def _gcp_subnets(client: httpx.Client, token: str, project: str, vpc_name: str) -> list[str]:
    # VPC는 전역 자원이다. 다른 리전과 보조 대역도 겹침 검사에 포함한다.
    url = f"{GCP_COMPUTE}/projects/{quote(project, safe='')}/aggregated/subnetworks"
    query = {}
    seen = set()
    subnets = []
    while True:
        page = _get(client, url, token, "GCP subnetworks", query)
        if page.get("warning") or page.get("unreachables"):
            raise LookupFailed("GCP subnetworks returned an incomplete listing")
        for scope in page.get("items", {}).values():
            warning = scope.get("warning")
            if warning and warning.get("code") != "NO_RESULTS_ON_PAGE":
                raise LookupFailed("GCP subnetworks returned an incomplete listing")
            for subnet in scope.get("subnetworks", []):
                if _gcp_network_is(subnet.get("network"), project, vpc_name):
                    subnets.append(subnet["ipCidrRange"])
                    subnets.extend(r["ipCidrRange"] for r in subnet.get("secondaryIpRanges", []))
        next_token = page.get("nextPageToken")
        if not next_token:
            break
        if not isinstance(next_token, str) or next_token in seen:
            raise LookupFailed("GCP subnetworks returned invalid pagination")
        seen.add(next_token)
        query["pageToken"] = next_token
    if not subnets:
        raise LookupFailed("the selected VPC has no subnets")
    return list(dict.fromkeys(subnets))


def _gcp_psa_ranges(client: httpx.Client, token: str, project: str, vpc_name: str, private_ip: str) -> list[str]:
    url = f"{GCP_COMPUTE}/projects/{quote(project, safe='')}/global/addresses"
    params = {"filter": 'purpose="VPC_PEERING"'}
    ip = ipaddress.ip_address(private_ip)
    ranges = []
    for address in _gcp_pages(client, url, token, "GCP global addresses", params):
        if address.get("purpose") != "VPC_PEERING" or not _gcp_network_is(address.get("network"), project, vpc_name):
            continue
        cidr = f"{address['address']}/{address['prefixLength']}"
        ranges.append(str(ipaddress.ip_network(cidr, strict=False)))
    if any(ip in ipaddress.ip_network(cidr) for cidr in ranges):
        return list(dict.fromkeys(ranges))
    raise LookupFailed("no private service access range holds the DB address; PSC-only instances are not supported")


# ---- Azure ----


def _azure_token(client: httpx.Client, credentials: dict[str, str]) -> str:
    response = client.post(
        f"https://login.microsoftonline.com/{quote(credentials['tenant_id'], safe='')}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "scope": f"{AZURE_ARM}/.default",
        },
    )
    return _json_or_fail(response, "Azure sign-in")["access_token"]


def _azure_location(value: str) -> str:
    """API는 Korea Central로, 입력은 koreacentral로 쓴다."""
    return value.replace(" ", "").lower()


def _azure_pages(client: httpx.Client, url: str, token: str, what: str, api_version: str) -> Iterator[dict]:
    params: dict[str, str] | None = {"api-version": api_version}
    next_url: str | None = url
    seen = set()
    while next_url:
        parsed = httpx.URL(next_url)
        if parsed.scheme != "https" or parsed.host != "management.azure.com" or parsed.port not in (None, 443):
            raise LookupFailed("Azure returned an invalid pagination address")
        if next_url in seen:
            raise LookupFailed("Azure returned invalid pagination")
        seen.add(next_url)
        page = _get(client, next_url, token, what, params)
        yield from page.get("value", [])
        # nextLink에는 api-version이 이미 들어 있다.
        next_url, params = page.get("nextLink"), None


def _azure_get(client: httpx.Client, resource_id: str, token: str, what: str, api_version: str) -> dict:
    if not isinstance(resource_id, str) or not resource_id.lower().startswith("/subscriptions/"):
        raise LookupFailed(f"{what} has an invalid resource ID")
    return _get(client, f"{AZURE_ARM}{quote(resource_id, safe='/')}", token, what, {"api-version": api_version})


def _find_azure(region: str, credentials: dict[str, str]) -> FoundEndpoint:
    subscription = credentials["subscription_id"]
    url = f"{AZURE_ARM}/subscriptions/{quote(subscription, safe='')}/providers/Microsoft.DBforPostgreSQL/flexibleServers"
    with _http() as client:
        token = _azure_token(client, credentials)
        servers = [s for s in _azure_pages(client, url, token, "Azure PostgreSQL servers", AZURE_POSTGRES_API)
                   if _azure_location(s.get("location", "")) == _azure_location(region)
                   and s.get("properties", {}).get("replica", {}).get("role") not in ("AsyncReplica", "GeoAsyncReplica")
                   and s.get("properties", {}).get("replicationRole") not in ("AsyncReplica", "GeoAsyncReplica")]
        server = _only_one(servers, region)
        server = _azure_get(client, server["id"], token, "Azure PostgreSQL server", AZURE_POSTGRES_API)
        properties = server.get("properties", {})
        if properties.get("state") != "Ready":
            raise LookupFailed("the Azure PostgreSQL server is not ready")
        endpoint = _azure_private_endpoint(client, token, properties)
        vnet_id = endpoint["subnet_id"].split("/subnets/")[0]
        vnet = _azure_get(client, vnet_id, token, "Azure virtual network", AZURE_NETWORK_API)
        wal_level = _azure_get(client, f"{server['id']}/configurations/wal_level", token,
                               "Azure server parameter", AZURE_POSTGRES_API)

    address_space = vnet.get("properties", {}).get("addressSpace", {}).get("addressPrefixes", [])
    if not address_space:
        raise LookupFailed("the virtual network of the DB has no address range")
    subnet_names = [s.get("name") for s in vnet.get("properties", {}).get("subnets", [])]
    resource_group = AZURE_RESOURCE_GROUP.search(vnet_id)
    return FoundEndpoint(
        cloud="azure",
        region=region,
        scope_id=subscription,
        db=FoundDb(
            instance=server["name"],
            host=endpoint["private_ip"],
            port=POSTGRES_PORT,
            version=properties.get("version"),
            publicly_accessible={"Enabled": True, "Disabled": False}.get(properties.get("network", {}).get("publicNetworkAccess")),
            logical_replication=_azure_logical_replication(wal_level),
        ),
        cidrs=tuple(address_space),
        network={
            "resource_group": resource_group.group(1) if resource_group else None,
            "vnet_name": vnet.get("name"),
            "address_space": address_space,
            "pe_subnet_id": endpoint["subnet_id"],
            "has_gateway_subnet": "GatewaySubnet" in subnet_names,
        },
    )


def _azure_private_endpoint(client: httpx.Client, token: str, server_properties: dict) -> dict[str, str]:
    """이 DB에는 Private Endpoint로 닿는다. 사설 DNS는 OCI에서 풀리지 않아 IP를 쓴다."""
    connections = server_properties.get("privateEndpointConnections") or []
    approved = [
        c["properties"]["privateEndpoint"]["id"]
        for c in connections
        if c.get("properties", {}).get("privateLinkServiceConnectionState", {}).get("status") == "Approved"
        and c.get("properties", {}).get("privateEndpoint", {}).get("id")
    ]
    if not approved:
        raise LookupFailed("the PostgreSQL server has no approved private endpoint; delegated-subnet-only servers are not supported")
    approved = list(dict.fromkeys(approved))
    if len(approved) != 1:
        raise LookupFailed("the PostgreSQL server has multiple approved private endpoints; exactly one is required")
    endpoint = _azure_get(client, approved[0], token, "Azure private endpoint", AZURE_NETWORK_API).get("properties", {})
    interfaces = endpoint.get("networkInterfaces") or []
    subnet_id = endpoint.get("subnet", {}).get("id")
    if len(interfaces) != 1 or not subnet_id:
        raise LookupFailed("the private endpoint of the PostgreSQL server is not ready")
    interface = _azure_get(client, interfaces[0]["id"], token, "Azure network interface", AZURE_NETWORK_API)
    addresses = [c.get("properties", {}).get("privateIPAddress") for c in interface.get("properties", {}).get("ipConfigurations", [])]
    addresses = list(dict.fromkeys(address for address in addresses if address))
    if len(addresses) != 1:
        raise LookupFailed("the private endpoint of the PostgreSQL server has no address")
    private_ip = addresses[0]
    if not ipaddress.ip_address(private_ip).is_private:
        raise LookupFailed("the private endpoint of the PostgreSQL server has no private address")
    return {"private_ip": private_ip, "subnet_id": subnet_id}


def _azure_logical_replication(parameter: dict) -> bool | None:
    properties = parameter.get("properties", {})
    if "value" not in properties:
        return None
    return properties["value"] == "logical" and properties.get("isConfigPendingRestart") is not True


_LOOKUPS: dict[str, Callable[[str, dict[str, str]], FoundEndpoint]] = {
    "aws": _find_aws,
    "gcp": _find_gcp,
    "azure": _find_azure,
}
