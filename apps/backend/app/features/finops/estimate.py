"""비용 산출: 원본·목적지 크기 × 각 클라우드 공식 가격 API 단가 → FinopsInput.

- 단가는 실행할 때 가격 API 에서 받습니다 (AWS Price List API · GCP Cloud Billing Catalog API · Azure Retail Prices API).
  표에 적어 둔 숫자가 아니므로 출처(`source`)에 API 이름과 조회 날짜를 남깁니다.
- 곱셈·합계는 이 파일이 합니다. 월 환산은 클라우드 가격표들이 쓰는 730시간입니다.
- 단가를 못 받거나 크기를 모르면 그 항목만 None("미제공") 입니다. 0 으로 채우지 않습니다.
  service.py 가 None 이 섞인 후보는 절감액을 내지 않으므로 싸 보이는 가짜 비교가 생기지 않습니다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from .schemas import CloudCost, CostLine, FinopsInput

log = logging.getLogger(__name__)

HOURS_PER_MONTH = 730
APP = "앱 컴퓨팅"
DB = "DB 컴퓨팅"
STORAGE = "DB 스토리지"


class Candidate(BaseModel):
    cloud: str  # "gcp" | "azure"
    region: str  # 예: "asia-northeast3", "koreacentral"


class CostRequest(BaseModel):
    """비용을 낼 때 필요한 크기. assessment 결과에서 채웁니다. 모르면 None."""

    source_region: str  # 원본 AWS 리전. 예: "ap-northeast-2"
    app_vcpu: float | None = None
    app_memory_mib: int | None = None
    app_instances: int = Field(default=1, ge=1)  # 항상 떠 있는 인스턴스 수. assessment 가 아직 안 읽음
    db_instance_class: str | None = None  # 원본 RDS 등급. 예: "db.t3.micro"
    db_vcpu: int | None = None
    db_memory_gib: float | None = None
    db_storage_gb: int | None = None
    candidates: list[Candidate] = Field(default_factory=list)


@dataclass(frozen=True)
class UnitPrice:
    usd: float  # 단위당 USD
    unit: str  # 예: "vCPU-hour", "GB-month"
    sku: str  # 가격표에서 고른 항목 이름 (확인용)


class PriceUnavailable(Exception):
    """가격 API 에서 단가를 확정하지 못함 (0건이거나 여러 건이 애매하게 걸림)."""


def _line(item: str, source: str, compute: Callable[[], float | None]) -> CostLine:
    """한 항목을 계산합니다. 크기를 모르거나 단가 조회가 실패하면 금액은 None 입니다."""
    try:
        value = compute()
    except Exception as exc:  # 한 항목 실패가 다른 항목을 막지 않게
        log.warning("finops 단가 조회 실패 %s (%s): %s", item, source, exc)
        return CostLine(item=item, monthly_usd=None, source=f"{source} (조회 실패: {type(exc).__name__})")
    return CostLine(item=item, monthly_usd=None if value is None else round(value, 2), source=source)


def _unsupported(cand: Candidate) -> CloudCost:
    return CloudCost(cloud=cand.cloud, region=cand.region, lines=[
        CostLine(item=name, monthly_usd=None, source="가격 조회를 지원하지 않는 클라우드") for name in (APP, DB, STORAGE)])


def build_input(req: CostRequest, aws: AwsPrices | None = None, gcp: GcpPrices | None = None,
                azure: AzurePrices | None = None, today: date | None = None) -> FinopsInput:
    """원본(AWS)과 후보들의 월 비용 항목을 가격 API 단가로 계산해 FinopsInput 으로 만듭니다."""
    stamp = (today or date.today()).isoformat()
    current = (aws or AwsPrices()).cost(req, f"aws-price-list-api {stamp}")
    candidates = []
    for cand in req.candidates:
        if cand.cloud == "gcp":
            candidates.append((gcp or GcpPrices()).cost(req, cand.region, f"gcp-cloud-billing-catalog {stamp}"))
        elif cand.cloud == "azure":
            candidates.append((azure or AzurePrices()).cost(req, cand.region, f"azure-retail-prices-api {stamp}"))
        else:
            candidates.append(_unsupported(cand))
    return FinopsInput(current=current, candidates=candidates)


# -- 공통 ------------------------------------------------------------------------------

HTTP_TIMEOUT_S = 30
_REGION = re.compile(r"^[a-z0-9-]{2,40}$")  # 필터 문자열에 넣기 전에 리전 이름 모양을 확인합니다


def _region(value: str) -> str:
    if not _REGION.match(value):
        raise ValueError(f"리전 이름 형식이 아닙니다: {value[:40]!r}")
    return value


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "alpha-finops", **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        return json.load(response)


def _one(prices: list[UnitPrice], what: str) -> UnitPrice:
    """후보가 하나로 좁혀져야 씁니다. 같은 값이 여러 줄이면 하나로 봅니다."""
    distinct = {p.usd for p in prices}
    if len(distinct) != 1:
        raise PriceUnavailable(f"{what}: 단가 {len(distinct)}개")
    return prices[0]


# -- Azure: Retail Prices API (인증 없음) ------------------------------------------------

AZURE_PRICES = "https://prices.azure.com/api/retail/prices"
AZURE_ACA_VCPU = "Standard vCPU Active Usage"  # 단위 "1 Second" = vCPU-초
AZURE_ACA_MEMORY = "Standard Memory Active Usage"  # 단위 "1 GiB Second"
AZURE_PG_STORAGE_PRODUCT = "Azure Database for PostgreSQL Flex Server Storage"
# PostgreSQL Flexible Server 등급: (vCPU, 메모리 GiB, armSkuName). 원본 크기 이상인 가장 작은 등급을 고릅니다.
# 이름 규칙이 등급마다 달라서(B1MS·B2S 는 접두어 없음) 가격 API 에 실제로 있는 값을 그대로 씁니다.
AZURE_PG_SKUS = [
    (1, 2, "B1MS"),
    (2, 4, "B2S"),
    (2, 8, "Standard_B2ms"),
    (4, 16, "Standard_D4ds_v5"),
    (8, 32, "Standard_D8ds_v5"),
    (16, 64, "Standard_D16ds_v5"),
]


def azure_query(odata_filter: str) -> list[dict]:
    url: str | None = AZURE_PRICES + "?" + urllib.parse.urlencode(
        {"currencyCode": "USD", "$filter": odata_filter}, quote_via=urllib.parse.quote)
    rows: list[dict] = []
    while url:  # 한 쪽에 1000건, NextPageLink 를 따라감
        data = _get_json(url)
        rows += data.get("Items", [])
        url = data.get("NextPageLink")
    return rows


def _azure_prices(rows: list[dict], unit: str) -> list[UnitPrice]:
    return [UnitPrice(float(r["retailPrice"]), unit, f"{r['productName']} / {r['meterName']}")
            for r in rows if r.get("type") == "Consumption"]


def azure_pg_sku(vcpu: int | None, memory_gib: float | None) -> str | None:
    if vcpu is None or memory_gib is None:
        return None
    for sku_vcpu, sku_mem, name in AZURE_PG_SKUS:
        if sku_vcpu >= vcpu and sku_mem >= memory_gib:
            return name
    return None


class AzurePrices:
    def __init__(self, query: Callable[[str], list[dict]] = azure_query):
        self.query = query

    def _meter(self, region: str, service: str, extra: str, unit: str) -> UnitPrice:
        rows = self.query(f"serviceName eq '{service}' and armRegionName eq '{_region(region)}' "
                          f"and type eq 'Consumption' and {extra}")
        return _one(_azure_prices(rows, unit), extra)

    def cost(self, req: CostRequest, region: str, source: str) -> CloudCost:
        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            # Container Apps 소비 요금. 항상 떠 있는 복제본을 '활성' 요금으로 셉니다 (보수적으로 비싸게)
            vcpu = self._meter(region, "Azure Container Apps", f"skuName eq 'Standard' and meterName eq '{AZURE_ACA_VCPU}'",
                               "vCPU-second")
            mem = self._meter(region, "Azure Container Apps", f"skuName eq 'Standard' and meterName eq '{AZURE_ACA_MEMORY}'",
                              "GiB-second")
            seconds = HOURS_PER_MONTH * 3600 * req.app_instances
            return seconds * (req.app_vcpu * vcpu.usd + req.app_memory_mib / 1024 * mem.usd)

        def db() -> float | None:
            sku = azure_pg_sku(req.db_vcpu, req.db_memory_gib)
            if sku is None:
                return None
            hourly = self._meter(region, "Azure Database for PostgreSQL", f"armSkuName eq '{sku}'", "hour")
            return HOURS_PER_MONTH * hourly.usd

        def storage() -> float | None:
            if req.db_storage_gb is None:
                return None
            per_gb = self._meter(region, "Azure Database for PostgreSQL",
                                 f"productName eq '{AZURE_PG_STORAGE_PRODUCT}' and skuName eq 'Storage'", "GB-month")
            return req.db_storage_gb * per_gb.usd

        sku = azure_pg_sku(req.db_vcpu, req.db_memory_gib)
        return CloudCost(cloud="azure", region=region, lines=[
            _line(APP, f"{source} Container Apps", app),
            _line(DB, f"{source} PostgreSQL Flexible {sku or '등급 미정'}", db),
            _line(STORAGE, f"{source} PostgreSQL Flexible Storage", storage),
        ])


# -- AWS: Price List Query API (pricing:GetProducts, 엔드포인트는 us-east-1 이지만 모든 리전 가격을 줌) ---------

AWS_PRICING_REGION = "us-east-1"
# Linux/x86 Fargate 는 운영체제·아키텍처 속성이 없어서 usagetype 으로 고릅니다 (ARM·Windows·EC2 행과 구분)
_FARGATE_VCPU = re.compile(r"^[A-Z0-9]+-Fargate-vCPU-Hours:perCPU$")
_FARGATE_GB = re.compile(r"^[A-Z0-9]+-Fargate-GB-Hours$")
# 같은 필터에 예약 용량용 'UnusedBox' 행이 하나 더 걸리므로 usagetype 으로 거릅니다
_RDS_SINGLE_AZ = "-InstanceUsage:"
AWS_RDS_GP3 = "General Purpose-GP3"


def _f(field: str, value: str) -> dict:
    return {"Type": "TERM_MATCH", "Field": field, "Value": value}


def _aws_ondemand(item: dict) -> list[float]:
    return [float(dim["pricePerUnit"]["USD"])
            for term in item.get("terms", {}).get("OnDemand", {}).values()
            for dim in term.get("priceDimensions", {}).values()]


class AwsPrices:
    def __init__(self, client=None):
        self._client = client

    def _products(self, service_code: str, filters: list[dict]) -> list[dict]:
        if self._client is None:
            import boto3  # assessment 와 같은 의존성. 키는 boto3 기본 경로(환경변수·프로필)에서 읽음

            self._client = boto3.client("pricing", region_name=AWS_PRICING_REGION)
        items = []
        for page in self._client.get_paginator("get_products").paginate(
                ServiceCode=service_code, Filters=filters, FormatVersion="aws_v1"):
            items += [json.loads(raw) for raw in page["PriceList"]]
        return items

    def _pick(self, items: list[dict], keep: Callable[[str], bool], unit: str, what: str) -> UnitPrice:
        prices = [UnitPrice(usd, unit, item["product"]["attributes"].get("usagetype", ""))
                  for item in items if keep(item["product"]["attributes"].get("usagetype", ""))
                  for usd in _aws_ondemand(item)]
        return _one(prices, what)

    def fargate(self, region: str) -> tuple[UnitPrice, UnitPrice]:
        region = _region(region)
        vcpu = self._pick(self._products("AmazonECS", [_f("regionCode", region), _f("cputype", "perCPU")]),
                          lambda u: bool(_FARGATE_VCPU.match(u)), "vCPU-hour", "fargate vCPU")
        mem = self._pick(self._products("AmazonECS", [_f("regionCode", region), _f("memorytype", "perGB")]),
                         lambda u: bool(_FARGATE_GB.match(u)), "GB-hour", "fargate GB")
        return vcpu, mem

    def rds_instance(self, region: str, instance_class: str) -> UnitPrice:
        items = self._products("AmazonRDS", [
            _f("regionCode", _region(region)), _f("productFamily", "Database Instance"),
            _f("databaseEngine", "PostgreSQL"), _f("instanceType", instance_class), _f("deploymentOption", "Single-AZ")])
        return self._pick(items, lambda u: _RDS_SINGLE_AZ in u, "hour", f"rds {instance_class}")

    def rds_storage(self, region: str) -> UnitPrice:
        items = self._products("AmazonRDS", [
            _f("regionCode", _region(region)), _f("productFamily", "Database Storage"),
            _f("databaseEngine", "PostgreSQL"), _f("volumeType", AWS_RDS_GP3), _f("deploymentOption", "Single-AZ")])
        return self._pick(items, lambda u: True, "GB-month", "rds gp3 storage")

    def cost(self, req: CostRequest, source: str) -> CloudCost:
        region = req.source_region

        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            vcpu, mem = self.fargate(region)
            hours = HOURS_PER_MONTH * req.app_instances
            return hours * (req.app_vcpu * vcpu.usd + req.app_memory_mib / 1024 * mem.usd)

        def db() -> float | None:
            if not req.db_instance_class:
                return None
            return HOURS_PER_MONTH * self.rds_instance(region, req.db_instance_class).usd

        def storage() -> float | None:
            if req.db_storage_gb is None:
                return None
            return req.db_storage_gb * self.rds_storage(region).usd

        return CloudCost(cloud="aws", region=region, lines=[
            _line(APP, f"{source} Fargate Linux/x86", app),
            _line(DB, f"{source} RDS PostgreSQL {req.db_instance_class or '등급 미제공'} Single-AZ", db),
            _line(STORAGE, f"{source} RDS gp3", storage),
        ])


# -- GCP: Cloud Billing Catalog API -----------------------------------------------------------------
# 인증: ALPHA_GCP_PRICING_API_KEY(API 키) 가 있으면 그것, 없으면 ADC(gcloud auth application-default login).
# 공개 가격표라 결제 계정 권한은 필요 없습니다.

GCP_CATALOG = "https://cloudbilling.googleapis.com/v1/services/{service}/skus"
GCP_KEY_ENV = "ALPHA_GCP_PRICING_API_KEY"
GCP_CLOUD_RUN = "152E-C115-5142"
GCP_CLOUD_SQL = "9662-B51E-5089"
# Cloud Run 은 category 가 모두 같아 설명 문구로 고릅니다. 항상 떠 있는 인스턴스라 '인스턴스 기반 과금' 단가입니다.
# 설명에 "in in" 오타가 있는 리전이 있어 앞부분만 맞춥니다.
_RUN_CPU = re.compile(r"^Services CPU \(Instance-based billing\) in ")
_RUN_MEMORY = re.compile(r"^Services Memory \(Instance-based billing\) in ")
# Cloud SQL Enterprise 단일 영역(Zonal). "Postgres"·"PostgreSQL" 표기가 섞여 있고, 체험판·N4·Enterprise Plus 행은 ^ 로 걸러집니다.
_SQL = r"^Cloud SQL for Postgre(?:SQL|s): Zonal\s*-\s*{what} in "
_SQL_VCPU = re.compile(_SQL.format(what="vCPU"))
_SQL_RAM = re.compile(_SQL.format(what="RAM"))
_SQL_SSD = re.compile(_SQL.format(what="Standard storage"))


def _gcp_auth_header() -> tuple[dict[str, str], dict[str, str]]:
    """(쿼리 파라미터, 헤더). 키가 있으면 키, 없으면 ADC 토큰."""
    key = os.environ.get(GCP_KEY_ENV, "").strip()
    if key:
        return {"key": key}, {}
    import google.auth  # ADC. provisioning 과 같은 로그인
    import google.auth.transport.requests

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-billing.readonly"])
    credentials.refresh(google.auth.transport.requests.Request())
    return {}, {"Authorization": f"Bearer {credentials.token}"}


def gcp_list_skus(service: str) -> list[dict]:
    params, headers = _gcp_auth_header()
    skus: list[dict] = []
    page = ""
    while True:  # Cloud SQL 은 2만 건이 넘어 여러 쪽입니다
        query = {"pageSize": 5000, "currencyCode": "USD", **params, **({"pageToken": page} if page else {})}
        data = _get_json(GCP_CATALOG.format(service=service) + "?" + urllib.parse.urlencode(query), headers)
        skus += data.get("skus", [])
        page = data.get("nextPageToken", "")
        if not page:
            return skus


def _gcp_unit_price(sku: dict) -> float:
    tiers = sku["pricingInfo"][-1]["pricingExpression"]["tieredRates"]
    # 무료 구간이 앞에 붙은 SKU 는 쓰지 않습니다 (CPU·메모리·DB 는 구간이 하나)
    if len(tiers) != 1:
        raise PriceUnavailable(f"{sku.get('description')}: 가격 구간 {len(tiers)}개")
    price = tiers[0]["unitPrice"]
    return int(price.get("units") or 0) + int(price.get("nanos") or 0) / 1e9


class GcpPrices:
    def __init__(self, list_skus: Callable[[str], list[dict]] = gcp_list_skus):
        self.list_skus = list_skus
        self._cache: dict[str, list[dict]] = {}

    def _skus(self, service: str) -> list[dict]:
        if service not in self._cache:
            self._cache[service] = self.list_skus(service)
        return self._cache[service]

    def _pick(self, service: str, region: str, pattern: re.Pattern, unit: str) -> UnitPrice:
        region = _region(region)
        hits = [s for s in self._skus(service)
                if region in s.get("serviceRegions", []) and s.get("category", {}).get("usageType") == "OnDemand"
                and pattern.search(s.get("description", ""))]
        if len(hits) != 1:
            raise PriceUnavailable(f"{pattern.pattern} @ {region}: {len(hits)}건")
        return UnitPrice(_gcp_unit_price(hits[0]), unit, hits[0].get("description", ""))

    def cost(self, req: CostRequest, region: str, source: str) -> CloudCost:
        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            cpu = self._pick(GCP_CLOUD_RUN, region, _RUN_CPU, "vCPU-second")
            mem = self._pick(GCP_CLOUD_RUN, region, _RUN_MEMORY, "GiB-second")
            seconds = HOURS_PER_MONTH * 3600 * req.app_instances
            return seconds * (req.app_vcpu * cpu.usd + req.app_memory_mib / 1024 * mem.usd)

        def db() -> float | None:
            if req.db_vcpu is None or req.db_memory_gib is None:
                return None
            vcpu = self._pick(GCP_CLOUD_SQL, region, _SQL_VCPU, "vCPU-hour")
            ram = self._pick(GCP_CLOUD_SQL, region, _SQL_RAM, "GiB-hour")
            return HOURS_PER_MONTH * (req.db_vcpu * vcpu.usd + req.db_memory_gib * ram.usd)

        def storage() -> float | None:
            if req.db_storage_gb is None:
                return None
            return req.db_storage_gb * self._pick(GCP_CLOUD_SQL, region, _SQL_SSD, "GiB-month").usd

        return CloudCost(cloud="gcp", region=region, lines=[
            _line(APP, f"{source} Cloud Run 인스턴스 기반", app),
            _line(DB, f"{source} Cloud SQL PostgreSQL Enterprise 단일 영역 {req.db_vcpu} vCPU·{req.db_memory_gib} GiB", db),
            _line(STORAGE, f"{source} Cloud SQL SSD", storage),
        ])
