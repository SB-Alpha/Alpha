"""비용 산출: 원본·목적지 크기 × 각 클라우드 공식 가격 API 단가 → FinopsInput.

- 단가는 실행할 때 가격 API 에서 받습니다 (AWS Price List API · GCP Cloud Billing Catalog API · Azure Retail Prices API).
  표에 적어 둔 숫자가 아니므로 출처(`source`)에 API 이름과 조회 날짜를 남깁니다.
- 곱셈·합계는 이 파일이 합니다. 월 환산은 클라우드 가격표들이 쓰는 730시간입니다.
- 단가를 못 받거나 크기를 모르면 그 항목만 None("미제공") 입니다. 0 으로 채우지 않습니다.
  service.py 가 None 이 섞인 후보는 절감액을 내지 않으므로 싸 보이는 가짜 비교가 생기지 않습니다.
- 트래픽 가정(`CostRequest.traffic`)을 주면 앱이 켜져 있는 시간을 배포 설정대로 셉니다.
  Cloud Run 은 min_instance_count = 0 이라 요청 없는 시간엔 0대, Azure 는 최소 복제본이 남아 유휴 요금.
  ECS Fargate 는 서비스 태스크가 계속 떠 있으므로 그대로입니다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .schemas import (
    Candidate,
    CloudCost,
    CostLine,
    CostRequest,
    Crossover,
    FinopsInput,
    Traffic,
)

log = logging.getLogger(__name__)

HOURS_PER_MONTH = 730
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
APP = "앱 컴퓨팅"
DB = "DB 컴퓨팅"
STORAGE = "DB 스토리지"


@dataclass(frozen=True)
class UnitPrice:
    usd: float  # 단위당 USD
    unit: str  # 예: "vCPU-hour", "GB-month"
    sku: str  # 가격표에서 고른 항목 이름 (확인용)


class PriceUnavailable(Exception):
    """가격 API 에서 단가를 확정하지 못함 (0건이거나 여러 건이 애매하게 걸림)."""


def is_burstable(req: CostRequest) -> bool:
    """원본 DB 가 CPU 를 나눠 쓰는 저가형(RDS db.t*)인지. 그렇다면 목적지도 저가형끼리 비교합니다.
    전용 vCPU 등급과 비교하면 같은 일을 하는데도 목적지가 몇 배 비싸게 나옵니다."""
    return bool(req.db_instance_class) and req.db_instance_class.startswith("db.t")


def _line(item: str, source: str, compute: Callable[[], float | None]) -> CostLine:
    """한 항목을 계산합니다. 크기를 모르거나 단가 조회가 실패하면 금액은 None 입니다."""
    try:
        value = compute()
    except Exception as exc:  # 한 항목 실패가 다른 항목을 막지 않게
        log.warning("finops 단가 조회 실패 %s (%s): %s", item, source, exc)
        return CostLine(item=item, monthly_usd=None, source=f"{source} (조회 실패: {type(exc).__name__})")
    return CostLine(item=item, monthly_usd=None if value is None else round(value, 2), source=source)


def active_share(traffic: Traffic | None) -> float:
    """한 달 중 요청이 있는 시간 비율. 가정이 없으면 1 (내내 켜짐)."""
    return 1.0 if traffic is None else 1 - traffic.idle_percent / 100


def assumptions(req: CostRequest) -> list[str]:
    t = req.traffic
    if t is None:
        return ["앱은 한 달 내내 켜져 있다고 보고 계산했습니다 (트래픽 가정 없음)."]
    return [
        f"트래픽 가정: 요청 없는 시간 {t.idle_percent:g}%, 요청 있는 시간 평균 {t.rps:g} RPS (실측 아님).",
        "AWS Fargate 는 서비스 태스크가 계속 떠 있어 내내 켜진 것으로 셉니다.",
        ("GCP Cloud Run 은 요청 없는 시간에 0대로 줄어든다고 봤고(min_instance_count = 0), "
         "마지막 요청 뒤 줄어들기까지 기다리는 시간은 빠져 있어 실제보다 조금 낮을 수 있습니다."),
        "Azure Container Apps 는 최소 복제본이 계속 떠 있어 요청 없는 시간은 유휴 요금으로 셉니다.",
    ]


def _unsupported(cand: Candidate) -> CloudCost:
    return CloudCost(cloud=cand.cloud, region=cand.region, lines=[
        CostLine(item=name, monthly_usd=None, source="가격 조회를 지원하지 않는 클라우드") for name in (APP, DB, STORAGE)])


def build_input(req: CostRequest, aws: AwsPrices | None = None, gcp: GcpPrices | None = None,
                azure: AzurePrices | None = None, today: date | None = None) -> FinopsInput:
    """원본(AWS)과 후보들의 월 비용 항목을 가격 API 단가로 계산해 FinopsInput 으로 만듭니다."""
    stamp = (today or date.today()).isoformat()
    aws = aws or AwsPrices()
    gcp = gcp or GcpPrices()
    current = aws.cost(req, f"{aws.api_name} {stamp}")
    candidates = []
    for cand in req.candidates:
        if cand.cloud == "gcp":
            candidates.append(gcp.cost(req, cand.region, f"gcp-cloud-billing-catalog {stamp}"))
        elif cand.cloud == "azure":
            candidates.append((azure or AzurePrices()).cost(req, cand.region, f"azure-retail-prices-api {stamp}"))
        else:
            candidates.append(_unsupported(cand))
    gcp_region = next((c.region for c in req.candidates if c.cloud == "gcp"), None)
    cross = crossover(req, aws, gcp, gcp_region) if gcp_region else None
    notes = assumptions(req) + (crossover_notes(cross, req) if cross else [])
    return FinopsInput(current=current, candidates=candidates, assumptions=notes, crossover=cross)


# -- 교차점: Cloud Run(요청 없으면 0대) 과 Fargate(내내 켜짐) -------------------------------------------
# 앱 컴퓨팅만 비교합니다. DB·스토리지는 트래픽과 상관없이 같은 값이라 교차점에 영향이 없습니다.

# Terraform(infra/terraform/gcp/modules/app/main.tf)이 max_instance_request_concurrency 를 적지 않아 Cloud Run 기본값입니다
CLOUD_RUN_CONCURRENCY = 80
CLOUD_RUN_MAX_INSTANCES = 2  # 같은 파일의 max_instance_count


def crossover(req: CostRequest, aws: AwsPrices, gcp: GcpPrices, region: str) -> Crossover:
    def not_provided(reason: str) -> Crossover:
        return Crossover(fargate_app_monthly_usd=None, cloud_run_instance_monthly_usd=None, break_even_idle_percent=None,
                         break_even_rps=None, concurrency=CLOUD_RUN_CONCURRENCY,
                         max_instances=CLOUD_RUN_MAX_INSTANCES, reason=reason)

    if req.app_vcpu is None or req.app_memory_mib is None:
        return not_provided("앱 크기가 미제공이라 교차점을 계산하지 않았습니다")
    try:
        fargate = aws.app_monthly(req)
        one = gcp.app_instance_month(req, region)
    except Exception as exc:  # noqa: BLE001 - 단가 하나가 없으면 교차점도 없습니다. 0 으로 채우지 않습니다
        log.warning("finops 교차점 단가 조회 실패 (%s): %s", type(exc).__name__, exc)
        return not_provided(f"단가 조회에 실패해 교차점을 계산하지 않았습니다 ({type(exc).__name__})")
    if one <= 0 or fargate <= 0:
        return not_provided("단가가 0 이라 교차점을 정할 수 없습니다")
    # 같은 대수끼리: Cloud Run 이 켜져 있는 비율이 fargate / (1대 한 달 × 대수) 보다 작으면 쌉니다
    idle = max(0.0, (1 - fargate / (one * req.app_instances)) * 100)
    rps, reason = _break_even_rps(req.traffic, fargate, one)
    return Crossover(fargate_app_monthly_usd=round(fargate, 2), cloud_run_instance_monthly_usd=round(one, 2),
                     break_even_idle_percent=round(idle, 1), break_even_rps=rps, concurrency=CLOUD_RUN_CONCURRENCY,
                     max_instances=CLOUD_RUN_MAX_INSTANCES, reason=reason)


def _break_even_rps(traffic: Traffic | None, fargate: float, one_instance_month: float) -> tuple[float | None, str | None]:
    """요청 있는 시간에 필요한 Cloud Run 대수 = ceil(RPS × 처리 시간 ÷ 동시 처리 수). Fargate 는 대수 고정.

    대수가 n 일 때 Cloud Run 비용은 n × 1대 한 달 × 켜진 비율입니다. Fargate 보다 싼 가장 큰 n 을 찾고,
    그 n 대가 감당하는 RPS 를 교차점으로 돌려줍니다. Terraform 한도를 넘는 값은 짐작하지 않습니다.
    """
    if traffic is None:
        return None, "트래픽 가정이 없어 RPS 교차점을 계산하지 않았습니다"
    if traffic.avg_latency_ms is None:
        return None, "요청당 처리 시간이 미제공이라 RPS 교차점을 계산하지 않았습니다"
    per_instance = one_instance_month * active_share(traffic)
    latency_s = traffic.avg_latency_ms / 1000
    cheaper = math.ceil(fargate / per_instance) - 1  # n × per_instance < fargate 인 가장 큰 n
    if cheaper < 1:
        return None, "이 유휴 비율에서는 Cloud Run 1대만 떠 있어도 Fargate 보다 비싸 RPS 교차점이 없습니다"
    if cheaper >= CLOUD_RUN_MAX_INSTANCES:
        cap = CLOUD_RUN_MAX_INSTANCES * CLOUD_RUN_CONCURRENCY / latency_s
        return None, (f"Cloud Run 최대 {CLOUD_RUN_MAX_INSTANCES}대(약 {cap:,.0f} RPS) 안에서는 교차점이 없습니다. "
                      "그 이상은 Terraform 한도라 계산하지 않습니다")
    return round(cheaper * CLOUD_RUN_CONCURRENCY / latency_s, 1), None


def crossover_notes(x: Crossover, req: CostRequest) -> list[str]:
    """교차점을 주의점 문장으로. 숫자는 위에서 계산한 값만 씁니다."""
    lines = []
    if x.break_even_idle_percent is not None:
        when = ("한 달 내내 켜져 있어도" if x.break_even_idle_percent == 0
                else f"유휴 {x.break_even_idle_percent:g}% 보다 길면")
        lines.append(f"교차점: 같은 크기·같은 대수라면 {when} Cloud Run 앱 비용이 Fargate 보다 작습니다 "
                     f"(Fargate 월 ${x.fargate_app_monthly_usd:,.2f}, Cloud Run 1대 한 달 내내 "
                     f"${x.cloud_run_instance_monthly_usd:,.2f}, 앱 컴퓨팅만).")
    if x.break_even_rps is not None and req.traffic is not None:
        lines.append(f"교차점: 유휴 {req.traffic.idle_percent:g}% 에서 요청 있는 시간 평균 {x.break_even_rps:g} RPS 이하이면 "
                     f"Cloud Run 앱 비용이 Fargate 보다 작습니다 (처리 시간 {req.traffic.avg_latency_ms:g}ms, "
                     f"인스턴스당 동시 {x.concurrency}건, Fargate {req.app_instances}대 고정 가정).")
    elif x.reason:
        lines.append(f"교차점: {x.reason}.")
    return lines


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


_CACHE_S = 6 * 3600
_cache: dict[tuple, tuple[float, object]] = {}
_cache_locks: dict[tuple, threading.Lock] = {}
_cache_guard = threading.Lock()


def _cached(key: tuple, fetch: Callable[[], object]) -> object:
    """가격표를 프로세스 안에 6시간 둡니다. 같은 표를 동시에 받으려 하면 먼저 받는 쪽을 기다립니다."""
    with _cache_guard:
        lock = _cache_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_S:
            return hit[1]
        value = fetch()
        _cache[key] = (time.monotonic(), value)
        return value


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
AZURE_ACA_VCPU_IDLE = "Standard vCPU Idle Usage"  # 복제본이 떠 있지만 요청을 처리하지 않을 때
AZURE_ACA_MEMORY_IDLE = "Standard Memory Idle Usage"
AZURE_ACA_REQUESTS = "Standard Requests"  # 단위 "1M"
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
    return _cached(("azure", odata_filter), lambda: _azure_fetch(odata_filter))


def _azure_fetch(odata_filter: str) -> list[dict]:
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


# 버스터블 원본용: (메모리 GiB, armSkuName). 저가형은 vCPU 를 나눠 쓰므로 메모리로 맞춥니다.
AZURE_PG_BURSTABLE = [(2, "B1MS"), (4, "B2S"), (8, "Standard_B2ms")]


def azure_pg_sku(vcpu: int | None, memory_gib: float | None, burstable: bool = False) -> str | None:
    if burstable and memory_gib is not None:
        return next((name for mem, name in AZURE_PG_BURSTABLE if mem >= memory_gib), None)
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

    def _aca(self, region: str, meter: str, unit: str) -> float:
        return self._meter(region, "Azure Container Apps", f"skuName eq 'Standard' and meterName eq '{meter}'", unit).usd

    def cost(self, req: CostRequest, region: str, source: str) -> CloudCost:
        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            # Container Apps 소비 요금. 요청이 있는 시간은 '활성', 복제본만 떠 있는 시간은 '유휴' 요금.
            # 트래픽 가정이 없으면 내내 활성으로 셉니다 (보수적으로 비싸게)
            gib = req.app_memory_mib / 1024
            active = (req.app_vcpu * self._aca(region, AZURE_ACA_VCPU, "vCPU-second")
                      + gib * self._aca(region, AZURE_ACA_MEMORY, "GiB-second"))
            share = active_share(req.traffic)
            total = SECONDS_PER_MONTH * req.app_instances * share * active
            if req.traffic is not None:
                idle = (req.app_vcpu * self._aca(region, AZURE_ACA_VCPU_IDLE, "vCPU-second")
                        + gib * self._aca(region, AZURE_ACA_MEMORY_IDLE, "GiB-second"))
                total += SECONDS_PER_MONTH * req.app_instances * (1 - share) * idle
                requests_m = req.traffic.rps * SECONDS_PER_MONTH * share / 1e6
                total += requests_m * self._aca(region, AZURE_ACA_REQUESTS, "1M requests")
            return total

        def db() -> float | None:
            sku = azure_pg_sku(req.db_vcpu, req.db_memory_gib, is_burstable(req))
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

        sku = azure_pg_sku(req.db_vcpu, req.db_memory_gib, is_burstable(req))
        return CloudCost(cloud="azure", region=region, lines=[
            _line(APP, f"{source} Container Apps" + (" 활성·유휴·요청 수수료" if req.traffic else " 내내 활성"), app),
            _line(DB, f"{source} PostgreSQL Flexible {sku or '등급 미정'}", db),
            _line(STORAGE, f"{source} PostgreSQL Flexible Storage", storage),
        ])


# -- AWS: Price List Bulk API (공개 파일) 또는 Query API (pricing:GetProducts) -------------------------

# Linux/x86 Fargate 는 운영체제·아키텍처 속성이 없어서 usagetype 으로 고릅니다 (ARM·Windows·EC2 행과 구분)
_FARGATE_VCPU = re.compile(r"^[A-Z0-9]+-Fargate-vCPU-Hours:perCPU$")
_FARGATE_GB = re.compile(r"^[A-Z0-9]+-Fargate-GB-Hours$")
# 같은 필터에 예약 용량용 'UnusedBox' 행이 하나 더 걸리므로 usagetype 으로 거릅니다
_RDS_SINGLE_AZ = "-InstanceUsage:"
AWS_RDS_GP3 = "General Purpose-GP3"
# 인터넷 송신(AWSDataTransfer). 같은 transferType 에 'Global-DataTransfer-Out-Bytes'(전 세계 합산 무료 100GB) 행이
# 있지만 fromRegionCode 가 비어 있어 리전 필터에서 빠집니다. 가속(…-ABytes)·피어링 행은 usagetype 으로 거릅니다
AWS_EGRESS_TRANSFER = "AWS Outbound"
_AWS_EGRESS = re.compile(r"^[A-Z0-9]+-DataTransfer-Out-Bytes$")


def _aws_first_paid_tier(item: dict) -> tuple[float, str] | None:
    """구간 요금(beginRange)에서 시작점이 가장 낮은 유료 구간의 (단가, 설명). 유료 구간이 없으면 None."""
    dims = [dim for term in item.get("terms", {}).get("OnDemand", {}).values()
            for dim in term.get("priceDimensions", {}).values()]
    for dim in sorted(dims, key=lambda d: float(d.get("beginRange") or 0)):
        usd = float(dim["pricePerUnit"]["USD"])
        if usd > 0:
            return usd, dim.get("description", "")
    return None


def _f(field: str, value: str) -> dict:
    return {"Type": "TERM_MATCH", "Field": field, "Value": value}


def _aws_ondemand(item: dict) -> list[float]:
    return [float(dim["pricePerUnit"]["USD"])
            for term in item.get("terms", {}).get("OnDemand", {}).values()
            for dim in term.get("priceDimensions", {}).values()]


AWS_BULK = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"


def _bulk_keep(service_code: str, product: dict) -> bool:
    """RDS 리전 파일은 20MB 라 PostgreSQL 인스턴스·스토리지만 남겨 메모리에 둡니다."""
    if service_code != "AmazonRDS":
        return True
    return (product.get("productFamily") in ("Database Instance", "Database Storage")
            and product.get("attributes", {}).get("databaseEngine") == "PostgreSQL")


def aws_bulk_items(service_code: str, region: str) -> list[dict]:
    """Price List Bulk API(공개 파일, 인증 없음) 의 리전 파일을 Query API 와 같은 모양(product + terms)으로."""
    return _cached(("aws", service_code, _region(region)), lambda: _aws_bulk_fetch(service_code, region))


def _aws_bulk_fetch(service_code: str, region: str) -> list[dict]:
    offer = _get_json(AWS_BULK.format(service=service_code, region=region))
    ondemand = offer.get("terms", {}).get("OnDemand", {})
    return [{"product": product, "terms": {"OnDemand": ondemand.get(sku, {})}}
            for sku, product in offer.get("products", {}).items() if _bulk_keep(service_code, product)]


def _matches(product: dict, filters: list[dict]) -> bool:
    for f in filters:
        field = f["Field"]
        value = product.get(field) if field == "productFamily" else product.get("attributes", {}).get(field)
        if value != f["Value"]:
            return False
    return True


class AwsPrices:
    """단가 출처는 두 가지입니다. 기본은 공개 가격 파일(Bulk API, 로그인 불필요),
    client(boto3 pricing) 를 주면 Query API 를 씁니다. 두 API 는 같은 가격 데이터를 같은 모양으로 줍니다."""

    def __init__(self, client=None, bulk: Callable[[str, str], list[dict]] = aws_bulk_items):
        self._client = client
        self._bulk = bulk
        self.api_name = "aws-price-list-query-api" if client is not None else "aws-price-list-bulk-api"

    def _products(self, service_code: str, filters: list[dict]) -> list[dict]:
        if self._client is None:
            # 데이터 송신(AWSDataTransfer) 행은 regionCode 가 없고 fromRegionCode 로 리전을 적습니다
            region = next(f["Value"] for f in filters if f["Field"] in ("regionCode", "fromRegionCode"))
            return [item for item in self._bulk(service_code, region) if _matches(item["product"], filters)]
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

    def internet_egress(self, region: str) -> UnitPrice:
        """리전에서 인터넷으로 나가는 송신 단가 (USD/GB). 구간 요금이라 첫 유료 구간을 씁니다."""
        items = self._products("AWSDataTransfer", [
            _f("fromRegionCode", _region(region)), _f("transferType", AWS_EGRESS_TRANSFER), _f("toLocation", "External")])
        prices = []
        for item in items:
            usagetype = item["product"]["attributes"].get("usagetype", "")
            tier = _aws_first_paid_tier(item) if _AWS_EGRESS.match(usagetype) else None
            if tier is not None:
                prices.append(UnitPrice(tier[0], "GB", f"{usagetype} ({tier[1]})"))
        return _one(prices, "aws internet egress")

    def app_monthly(self, req: CostRequest) -> float:
        """Fargate 앱 월 비용. 서비스 태스크가 계속 떠 있어 트래픽과 상관없이 내내 켜진 값입니다."""
        vcpu, mem = self.fargate(req.source_region)
        hours = HOURS_PER_MONTH * req.app_instances
        return hours * (req.app_vcpu * vcpu.usd + req.app_memory_mib / 1024 * mem.usd)

    def cost(self, req: CostRequest, source: str) -> CloudCost:
        region = req.source_region

        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            return self.app_monthly(req)

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
# Cloud Run 은 category 가 모두 같아 설명 문구로 고릅니다. '인스턴스 기반 과금' 단가입니다: Terraform 이
# resources.limits 를 적고 cpu_idle 은 안 적어서 CPU 가 항상 할당되는 방식입니다 (요청 수수료 없음).
# 설명에 "in in" 오타가 있는 리전이 있어 앞부분만 맞춥니다.
_RUN_CPU = re.compile(r"^Services CPU \(Instance-based billing\) in ")
_RUN_MEMORY = re.compile(r"^Services Memory \(Instance-based billing\) in ")
# Cloud SQL Enterprise 단일 영역(Zonal). "Postgres"·"PostgreSQL" 표기가 섞여 있고, 체험판·N4·Enterprise Plus 행은 ^ 로 걸러집니다.
_SQL = r"^Cloud SQL for Postgre(?:SQL|s): Zonal\s*-\s*{what} in "
_SQL_VCPU = re.compile(_SQL.format(what="vCPU"))
_SQL_RAM = re.compile(_SQL.format(what="RAM"))
_SQL_SSD = re.compile(_SQL.format(what="Standard storage"))
# 공유 코어 등급 (버스터블 원본용): (메모리 GiB, 가격표 이름, 등급 이름). db-f1-micro 0.6GB, db-g1-small 1.7GB
GCP_SQL_SHARED = [(0.6, "Micro instance", "db-f1-micro"), (1.7, "Small instance", "db-g1-small")]


def gcp_sql_shared(memory_gib: float | None) -> tuple[str, str] | None:
    if memory_gib is None:
        return None
    return next(((label, tier) for mem, label, tier in GCP_SQL_SHARED if mem >= memory_gib), None)


def _gcp_auth_header() -> tuple[dict[str, str], dict[str, str]]:
    """(쿼리 파라미터, 헤더). 키가 있으면 키, 없으면 ADC 토큰."""
    key = os.environ.get(GCP_KEY_ENV, "").strip()
    if key:
        return {"key": key}, {}
    import google.auth  # ADC. provisioning 과 같은 로그인
    import google.auth.transport.urllib3  # 의존성이 google-auth[urllib3] 라 requests 가 없습니다
    import urllib3

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-billing.readonly"])
    credentials.refresh(google.auth.transport.urllib3.Request(urllib3.PoolManager()))
    return {}, {"Authorization": f"Bearer {credentials.token}"}


def gcp_list_skus(service: str) -> list[dict]:
    return _cached(("gcp", service), lambda: _gcp_fetch(service))


def _gcp_fetch(service: str) -> list[dict]:
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
    return _gcp_money(tiers[0]["unitPrice"])


def _gcp_money(price: dict) -> float:
    return int(price.get("units") or 0) + int(price.get("nanos") or 0) / 1e9


def _gcp_first_paid_tier(sku: dict) -> tuple[float, float]:
    """구간 요금(startUsageAmount)에서 시작점이 가장 낮은 유료 구간의 (단가, 시작 사용량)."""
    tiers = sku["pricingInfo"][-1]["pricingExpression"]["tieredRates"]
    for tier in sorted(tiers, key=lambda t: float(t.get("startUsageAmount") or 0)):
        usd = _gcp_money(tier["unitPrice"])
        if usd > 0:
            return usd, float(tier.get("startUsageAmount") or 0)
    raise PriceUnavailable(f"{sku.get('description')}: 유료 구간 없음")


# Cloud Run 카탈로그 안의 인터넷 송신 SKU. 대륙 간(serviceRegions = global) 행은 리전 필터에서 빠지고,
# 리전마다 같은 대륙 안 송신 행 하나가 남습니다 (asia-northeast3 → "… AsiaPacific to AsiaPacific")
GCP_EGRESS_GROUP = "PremiumInternetEgress"
_RUN_EGRESS = re.compile(r"^Cloud Run Network Internet Data Transfer Out ")


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

    def internet_egress(self, region: str) -> UnitPrice:
        """Cloud Run 에서 인터넷으로 나가는 송신 단가 (USD/GiB). 구간 요금이라 첫 유료 구간을 씁니다."""
        region = _region(region)
        hits = [s for s in self._skus(GCP_CLOUD_RUN)
                if region in s.get("serviceRegions", []) and s.get("category", {}).get("usageType") == "OnDemand"
                and s.get("category", {}).get("resourceGroup") == GCP_EGRESS_GROUP
                and _RUN_EGRESS.search(s.get("description", ""))]
        if len(hits) != 1:
            raise PriceUnavailable(f"{_RUN_EGRESS.pattern} @ {region}: {len(hits)}건")
        usd, start = _gcp_first_paid_tier(hits[0])
        return UnitPrice(usd, "GiB", f"{hits[0].get('description', '')} ({start:g} GiB 부터 구간)")

    def app_instance_month(self, req: CostRequest, region: str) -> float:
        """Cloud Run 인스턴스 1대를 한 달 내내 켰을 때 (인스턴스 기반 과금)."""
        cpu = self._pick(GCP_CLOUD_RUN, region, _RUN_CPU, "vCPU-second")
        mem = self._pick(GCP_CLOUD_RUN, region, _RUN_MEMORY, "GiB-second")
        return SECONDS_PER_MONTH * (req.app_vcpu * cpu.usd + req.app_memory_mib / 1024 * mem.usd)

    def cost(self, req: CostRequest, region: str, source: str) -> CloudCost:
        def app() -> float | None:
            if req.app_vcpu is None or req.app_memory_mib is None:
                return None
            # 트래픽 가정이 있으면 요청이 있는 시간만 켜져 있습니다 (min_instance_count = 0)
            return self.app_instance_month(req, region) * req.app_instances * active_share(req.traffic)

        shared = gcp_sql_shared(req.db_memory_gib) if is_burstable(req) else None

        def db() -> float | None:
            if shared is not None:
                pattern = re.compile(_SQL.format(what=re.escape(shared[0])))
                return HOURS_PER_MONTH * self._pick(GCP_CLOUD_SQL, region, pattern, "hour").usd
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
            _line(APP, f"{source} Cloud Run 인스턴스 기반" + (" 요청 있는 시간만" if req.traffic else " 내내 켜짐"), app),
            _line(DB, f"{source} Cloud SQL PostgreSQL 단일 영역 "
                      + (f"{shared[1]} (공유 코어)" if shared else f"{req.db_vcpu} vCPU·{req.db_memory_gib} GiB"), db),
            _line(STORAGE, f"{source} Cloud SQL SSD", storage),
        ])


# -- 미리 받기 ----------------------------------------------------------------------------------------

WARMUP_ENV = "ALPHA_FINOPS_WARMUP"  # "0" 이면 끕니다
_warm_started = False
_warm_guard = threading.Lock()


def _warm_jobs(aws_region: str) -> list[Callable[[], object]]:
    return [lambda: aws_bulk_items("AmazonECS", aws_region), lambda: aws_bulk_items("AmazonRDS", aws_region),
            lambda: aws_bulk_items("AWSDataTransfer", aws_region),
            lambda: gcp_list_skus(GCP_CLOUD_RUN), lambda: gcp_list_skus(GCP_CLOUD_SQL)]


def warm_up(aws_region: str = "ap-northeast-2", jobs: list[Callable[[], object]] | None = None) -> bool:
    """서버가 뜰 때 큰 가격표(AWS 리전 파일, GCP 카탈로그)를 백그라운드로 받아 둡니다.
    첫 /finops/estimate 가 20초쯤 기다리지 않게 하려는 것입니다. 실패해도 요청 때 다시 받으므로 기록만 합니다.
    한 프로세스에서 한 번만 돕니다. 시작했으면 True."""
    global _warm_started
    if os.environ.get(WARMUP_ENV, "1") == "0":
        return False
    with _warm_guard:
        if _warm_started:
            return False
        _warm_started = True
    todo = jobs if jobs is not None else _warm_jobs(aws_region)

    def run() -> None:
        for job in todo:
            try:
                job()
            except Exception as exc:  # 로그인 없음·네트워크 문제는 요청 때 "미제공" 으로 드러납니다
                log.info("finops 가격표 미리 받기 실패: %s", exc)

    threading.Thread(target=run, name="finops-warmup", daemon=True).start()
    return True
