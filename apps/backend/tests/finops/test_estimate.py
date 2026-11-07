"""가격 API 응답 모양을 흉내 낸 가짜 응답으로 비용 산출을 확인합니다. 실제 네트워크는 쓰지 않습니다."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.features.finops import estimate, service
from app.features.finops.estimate import (
    AwsPrices,
    AzurePrices,
    Candidate,
    CostRequest,
    Traffic,
)

TODAY = date(2026, 11, 7)


def _req(**overrides) -> CostRequest:
    base = dict(source_region="ap-northeast-2", app_vcpu=1, app_memory_mib=2048, db_instance_class="db.t3.micro",
                db_vcpu=2, db_memory_gib=1, db_storage_gb=20,
                candidates=[Candidate(cloud="gcp", region="asia-northeast3"),
                            Candidate(cloud="azure", region="koreacentral")])
    return CostRequest(**{**base, **overrides})


# -- AWS: Price List API 응답 모양 (PriceList 원소는 JSON 문자열) ------------------------------------

def _aws_item(usagetype: str, usd: str) -> str:
    return json.dumps({
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"SKU.T": {"priceDimensions": {"SKU.T.R": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}},
    })


AWS_ROWS = {
    ("AmazonECS", "cputype"): [_aws_item("APN2-Fargate-vCPU-Hours:perCPU", "0.0465600000"),
                               _aws_item("APN2-Fargate-ARM-vCPU-Hours:perCPU", "0.0372500000"),
                               _aws_item("APN2-ECS-EC2-vCPU-Hours", "0.0100000000")],
    ("AmazonECS", "memorytype"): [_aws_item("APN2-Fargate-GB-Hours", "0.0051100000"),
                                  _aws_item("APN2-Fargate-ARM-GB-Hours", "0.0040900000")],
    ("AmazonRDS", "Database Instance"): [_aws_item("APN2-InstanceUsage:db.t3.micro", "0.0280000000"),
                                         _aws_item("APN2-UnusedBox:db.t3.micro", "0.0280000000")],
    ("AmazonRDS", "Database Storage"): [_aws_item("APN2-RDS:GP3-Storage", "0.1310000000")],
}


class FakePricingClient:
    def __init__(self, rows=AWS_ROWS):
        self.rows, self.calls = rows, []

    def get_paginator(self, name):
        assert name == "get_products"
        return self

    def paginate(self, ServiceCode, Filters, FormatVersion):
        self.calls.append((ServiceCode, {f["Field"]: f["Value"] for f in Filters}))
        fields = {f["Field"]: f["Value"] for f in Filters}
        key = (ServiceCode, fields.get("productFamily") or next(k for k in ("cputype", "memorytype") if k in fields))
        yield {"PriceList": self.rows.get(key, [])}


def test_aws_prices_pick_linux_fargate_and_single_az_rds():
    cost = AwsPrices(FakePricingClient()).cost(_req(), "aws-price-list-api 2026-11-07")
    app, db, storage = (line.monthly_usd for line in cost.lines)
    assert app == round(730 * (1 * 0.04656 + 2 * 0.00511), 2)  # ARM·EC2 행은 고르지 않음
    assert db == round(730 * 0.028, 2)  # UnusedBox 행은 빼고 한 건
    assert storage == round(20 * 0.131, 2)
    assert all("aws-price-list-api 2026-11-07" in line.source for line in cost.lines)


def test_aws_ambiguous_price_is_not_guessed():
    rows = {**AWS_ROWS, ("AmazonRDS", "Database Instance"): [
        _aws_item("APN2-InstanceUsage:db.t3.micro", "0.028"), _aws_item("APN2-InstanceUsage:db.t3.micro", "0.031")]}
    db = AwsPrices(FakePricingClient(rows)).cost(_req(), "aws").lines[1]
    assert db.monthly_usd is None and "조회 실패: PriceUnavailable" in db.source


def test_aws_unknown_instance_class_is_not_provided():
    db = AwsPrices(FakePricingClient()).cost(_req(db_instance_class=None), "aws").lines[1]
    assert db.monthly_usd is None and "등급 미제공" in db.source


# -- Azure: Retail Prices API 응답 모양 ------------------------------------------------------------

def _az(meter: str, price: float, kind: str = "Consumption", product: str = "p") -> dict:
    return {"retailPrice": price, "type": kind, "productName": product, "meterName": meter}


def fake_azure_query(odata_filter: str) -> list[dict]:
    if "Standard vCPU Active Usage" in odata_filter:
        return [_az("Standard vCPU Active Usage", 0.000024)]
    if "Standard Memory Active Usage" in odata_filter:
        return [_az("Standard Memory Active Usage", 0.000003)]
    if "Standard vCPU Idle Usage" in odata_filter:
        return [_az("Standard vCPU Idle Usage", 0.000003)]
    if "Standard Memory Idle Usage" in odata_filter:
        return [_az("Standard Memory Idle Usage", 0.000003)]
    if "Standard Requests" in odata_filter:
        return [_az("Standard Requests", 0.4)]
    if "armSkuName eq 'B1MS'" in odata_filter:
        return [_az("B1MS", 0.026)]
    if "armSkuName eq 'B2S'" in odata_filter:
        return [_az("B2S", 0.104), _az("B2S", 1240.0, kind="Reservation")]  # 예약 행은 기간 전체 금액
    if "Flex Server Storage" in odata_filter:
        return [_az("Storage Data Stored", 0.131)]
    return []


def test_azure_prices_use_consumption_rows_only():
    req = _req(db_instance_class="db.m6g.large")  # 전용 vCPU 원본 → vCPU·메모리로 등급 선택
    cost = AzurePrices(fake_azure_query).cost(req, "koreacentral", "azure-retail-prices-api 2026-11-07")
    app, db, storage = (line.monthly_usd for line in cost.lines)
    assert app == round(730 * 3600 * (1 * 0.000024 + 2 * 0.000003), 2)
    assert db == round(730 * 0.104, 2)
    assert storage == round(20 * 0.131, 2)
    assert "B2S" in cost.lines[1].source


def test_burstable_source_db_is_compared_with_burstable_targets():
    req = _req()  # db.t3.micro: 2 vCPU 를 나눠 쓰는 1 GiB 저가형
    assert estimate.is_burstable(req)
    azure = AzurePrices(fake_azure_query).cost(req, "koreacentral", "azure").lines[1]
    assert azure.monthly_usd == round(730 * 0.026, 2) and "B1MS" in azure.source
    gcp = estimate.GcpPrices(GCP_SKUS.__getitem__).cost(req, "asia-northeast3", "gcp").lines[1]
    assert gcp.monthly_usd == round(730 * 0.0455, 2) and "db-g1-small" in gcp.source


@pytest.mark.parametrize(("mem", "azure", "gcp"), [
    (0.5, "B1MS", ("Micro instance", "db-f1-micro")), (1, "B1MS", ("Small instance", "db-g1-small")),
    (4, "B2S", None), (16, None, None),
])
def test_burstable_sizes_follow_memory(mem, azure, gcp):
    assert estimate.azure_pg_sku(2, mem, burstable=True) == azure
    assert estimate.gcp_sql_shared(mem) == gcp


@pytest.mark.parametrize(("vcpu", "mem", "sku"), [
    (1, 1, "B1MS"), (2, 1, "B2S"), (2, 8, "Standard_B2ms"), (4, 8, "Standard_D4ds_v5"), (64, 512, None), (None, 4, None),
])
def test_azure_db_size_maps_to_the_smallest_sku_that_fits(vcpu, mem, sku):
    assert estimate.azure_pg_sku(vcpu, mem) == sku


def test_region_names_are_checked_before_going_into_a_filter():
    def must_not_run(_):
        raise AssertionError("query must not run")

    line = AzurePrices(must_not_run).cost(_req(app_vcpu=1), "x' or 1 eq 1", "azure").lines[0]
    assert line.monthly_usd is None and "ValueError" in line.source


# -- 전체 흐름 ----------------------------------------------------------------------------------

class FakeGcp:
    def cost(self, req, region, source):
        from app.features.finops.schemas import CloudCost, CostLine
        return CloudCost(cloud="gcp", region=region, lines=[
            CostLine(item=estimate.APP, monthly_usd=50.0, source=source),
            CostLine(item=estimate.DB, monthly_usd=None, source=f"{source} (조회 실패: RuntimeError)"),
            CostLine(item=estimate.STORAGE, monthly_usd=3.4, source=source),
        ])


def test_build_input_feeds_the_report_and_a_failed_lookup_blocks_the_saving():
    inp = estimate.build_input(_req(), aws=AwsPrices(FakePricingClient()), gcp=FakeGcp(),
                               azure=AzurePrices(fake_azure_query), today=TODAY)
    assert [c.cloud for c in inp.candidates] == ["gcp", "azure"]
    assert inp.current.lines[0].source.startswith("aws-price-list-query-api 2026-11-07")
    report = service.run(inp)
    gcp, azure = report.savings
    assert gcp.monthly_saving_usd is None and gcp.missing == ["gcp DB 컴퓨팅"]  # 조회 실패는 0 이 아니라 미제공
    assert azure.complete and azure.monthly_saving_usd is not None


def test_unsupported_cloud_is_reported_as_not_provided():
    inp = estimate.build_input(_req(candidates=[Candidate(cloud="oci", region="ap-seoul-1")]),
                               aws=AwsPrices(FakePricingClient()), today=TODAY)
    assert all(line.monthly_usd is None for line in inp.candidates[0].lines)


# -- GCP: Cloud Billing Catalog 응답 모양 -------------------------------------------------------------

def _sku(description: str, region: str, units: str, nanos: int, tiers: int = 1) -> dict:
    rates = [{"startUsageAmount": 0, "unitPrice": {"units": units, "nanos": nanos}}]
    if tiers == 2:
        rates = [{"startUsageAmount": 0, "unitPrice": {"units": "0", "nanos": 0}}, *rates]
    return {"description": description, "serviceRegions": [region], "category": {"usageType": "OnDemand"},
            "pricingInfo": [{"pricingExpression": {"tieredRates": rates}}]}


GCP_SKUS = {
    estimate.GCP_CLOUD_RUN: [
        _sku("Services CPU (Instance-based billing) in asia-northeast3", "asia-northeast3", "0", 21600),
        _sku("Services Memory (Instance-based billing) in asia-northeast3", "asia-northeast3", "0", 2400),
        _sku("Instances CPU in asia-northeast3", "asia-northeast3", "0", 99999),  # 다른 자원
        _sku("Services CPU (Instance-based billing) in asia-northeast1", "asia-northeast1", "0", 18000),
    ],
    estimate.GCP_CLOUD_SQL: [
        _sku("Cloud SQL for PostgreSQL: Zonal - vCPU in Seoul", "asia-northeast3", "0", 53700000),
        _sku("Cloud SQL for Postgres: Zonal - RAM in Seoul", "asia-northeast3", "0", 9100000),
        _sku("Cloud SQL for PostgreSQL: Zonal - Standard storage in Seoul", "asia-northeast3", "0", 221000000),
        _sku("Cloud SQL for PostgreSQL: Regional - vCPU in Seoul", "asia-northeast3", "0", 107400000),  # HA
        _sku("FDC Trial in Seoul: Cloud SQL for PostgreSQL: Zonal - vCPU", "asia-northeast3", "0", 53700000),
        _sku("Cloud SQL for PostgreSQL: Zonal - Small instance in Seoul", "asia-northeast3", "0", 45500000),
        _sku("FDC Trial in Cloud SQL for PostgreSQL: Zonal - Small instance in Seoul", "asia-northeast3", "0", 45500000),
    ],
}


def test_gcp_prices_pick_instance_based_cloud_run_and_zonal_cloud_sql():
    calls = []

    def list_skus(service):
        calls.append(service)
        return GCP_SKUS[service]

    cost = estimate.GcpPrices(list_skus).cost(_req(db_memory_gib=8), "asia-northeast3", "gcp")
    app, db, storage = (line.monthly_usd for line in cost.lines)
    assert app == round(730 * 3600 * (1 * 0.0000216 + 2 * 0.0000024), 2)
    assert db == round(730 * (2 * 0.0537 + 8 * 0.0091), 2)
    assert storage == round(20 * 0.221, 2)
    assert sorted(calls) == sorted(set(calls))  # 서비스마다 한 번만 받아 둠 (Cloud SQL 은 2만 건)


def test_gcp_free_tier_or_missing_region_is_not_guessed():
    skus = {**GCP_SKUS, estimate.GCP_CLOUD_RUN: [
        _sku("Services CPU (Instance-based billing) in asia-northeast3", "asia-northeast3", "0", 21600, tiers=2)]}
    cost = estimate.GcpPrices(skus.__getitem__).cost(_req(), "asia-northeast3", "gcp")
    assert cost.lines[0].monthly_usd is None
    other = estimate.GcpPrices(GCP_SKUS.__getitem__).cost(_req(), "europe-west1", "gcp")
    assert all(line.monthly_usd is None for line in other.lines)


def test_gcp_uses_the_api_key_when_set(monkeypatch):
    monkeypatch.setenv(estimate.GCP_KEY_ENV, "k")
    assert estimate._gcp_auth_header() == ({"key": "k"}, {})


def test_estimate_api_returns_the_priced_lines_and_the_report(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    real_build = estimate.build_input

    def fake_build(req):
        return real_build(req, aws=AwsPrices(FakePricingClient()), gcp=FakeGcp(),
                                    azure=AzurePrices(fake_azure_query), today=TODAY)

    monkeypatch.setattr(estimate, "build_input", fake_build)
    body = _req().model_dump(mode="json")
    res = TestClient(create_app()).post("/finops/estimate", json=body)
    assert res.status_code == 200
    data = res.json()
    assert data["input"]["current"]["cloud"] == "aws"
    assert [s["cloud"] for s in data["report"]["savings"]] == ["gcp", "azure"]


# -- AWS 공개 가격 파일(Bulk API) 경로 -------------------------------------------------------------

def _offer_product(sku, family, usagetype, **attrs):
    return sku, {"sku": sku, "productFamily": family,
                 "attributes": {"regionCode": "ap-northeast-2", "usagetype": usagetype, **attrs}}


def _offer(products: list[tuple[str, dict]], prices: dict[str, str]) -> dict:
    return {"products": dict(products), "terms": {"OnDemand": {
        sku: {f"{sku}.T": {"priceDimensions": {f"{sku}.T.R": {"pricePerUnit": {"USD": usd}}}}} for sku, usd in prices.items()}}}


OFFERS = {
    "AmazonECS": _offer([
        _offer_product("A", "Compute", "APN2-Fargate-vCPU-Hours:perCPU", cputype="perCPU"),
        _offer_product("B", "Compute", "APN2-Fargate-ARM-vCPU-Hours:perCPU", cputype="perCPU", cpuArchitecture="ARM"),
        _offer_product("C", "Compute", "APN2-Fargate-GB-Hours", memorytype="perGB"),
    ], {"A": "0.04656", "B": "0.03725", "C": "0.00511"}),
    "AmazonRDS": _offer([
        _offer_product("D", "Database Instance", "APN2-InstanceUsage:db.t3.micro", databaseEngine="PostgreSQL",
                       instanceType="db.t3.micro", deploymentOption="Single-AZ"),
        _offer_product("E", "Database Instance", "APN2-UnusedBox:db.t3.micro", databaseEngine="PostgreSQL",
                       instanceType="db.t3.micro", deploymentOption="Single-AZ"),
        _offer_product("F", "Database Instance", "APN2-InstanceUsage:db.t3.micro", databaseEngine="MySQL",
                       instanceType="db.t3.micro", deploymentOption="Single-AZ"),
        _offer_product("G", "Database Storage", "APN2-RDS:GP3-Storage", databaseEngine="PostgreSQL",
                       volumeType="General Purpose-GP3", deploymentOption="Single-AZ"),
    ], {"D": "0.028", "E": "0.028", "F": "0.025", "G": "0.131"}),
}


def test_aws_public_price_files_need_no_login(monkeypatch):
    fetched = []

    def fake_get_json(url, headers=None):
        fetched.append(url)
        service = url.split("/aws/")[1].split("/")[0]
        return OFFERS[service]

    monkeypatch.setattr(estimate, "_get_json", fake_get_json)
    monkeypatch.setattr(estimate, "_cache", {})
    prices = AwsPrices()
    cost = prices.cost(_req(), f"{prices.api_name} 2026-11-07")
    app, db, storage = (line.monthly_usd for line in cost.lines)
    assert app == round(730 * (1 * 0.04656 + 2 * 0.00511), 2)
    assert db == round(730 * 0.028, 2)  # MySQL·UnusedBox 행은 빼고
    assert storage == round(20 * 0.131, 2)
    assert cost.lines[0].source.startswith("aws-price-list-bulk-api")
    assert len(fetched) == 2  # 서비스·리전 파일을 한 번씩만 받아 둠


# -- 트래픽 가정 (Cloud Run 은 요청 없는 시간에 0대, Azure 는 유휴 요금) -------------------------------

def test_traffic_assumption_follows_how_each_cloud_is_deployed():
    traffic = Traffic(idle_percent=40, rps=10)
    req = _req(traffic=traffic)
    inp = estimate.build_input(req, aws=AwsPrices(FakePricingClient()), gcp=estimate.GcpPrices(GCP_SKUS.__getitem__),
                               azure=AzurePrices(fake_azure_query), today=TODAY)
    month = 730 * 3600
    aws_app = inp.current.lines[0].monthly_usd
    gcp_app = inp.candidates[0].lines[0].monthly_usd
    azure_app = inp.candidates[1].lines[0].monthly_usd
    assert aws_app == round(730 * (1 * 0.04656 + 2 * 0.00511), 2)  # Fargate 는 그대로 내내
    assert gcp_app == round(month * 0.6 * (1 * 0.0000216 + 2 * 0.0000024), 2)  # 요청 있는 60% 만
    active = month * 0.6 * (1 * 0.000024 + 2 * 0.000003)
    idle = month * 0.4 * (1 * 0.000003 + 2 * 0.000003)
    requests = 10 * month * 0.6 / 1e6 * 0.4
    assert azure_app == round(active + idle + requests, 2)
    assert "요청 있는 시간만" in inp.candidates[0].lines[0].source
    notes = service.run(inp).notes
    assert any("요청 없는 시간 40%" in n and "실측 아님" in n for n in notes)  # 가정은 주의점으로 드러남


def test_without_traffic_the_app_is_priced_as_always_on():
    inp = estimate.build_input(_req(), aws=AwsPrices(FakePricingClient()), gcp=estimate.GcpPrices(GCP_SKUS.__getitem__),
                               azure=AzurePrices(fake_azure_query), today=TODAY)
    assert inp.candidates[0].lines[0].monthly_usd == round(730 * 3600 * (1 * 0.0000216 + 2 * 0.0000024), 2)
    assert any("내내 켜져" in a for a in inp.assumptions)


@pytest.mark.parametrize("bad", [{"idle_percent": 100}, {"idle_percent": -1}, {"idle_percent": float("nan")},
                                 {"idle_percent": 10, "rps": -1}])
def test_impossible_traffic_assumptions_are_rejected(bad):
    with pytest.raises(ValidationError):
        Traffic(**bad)


# -- 가격표 보관과 미리 받기 -------------------------------------------------------------------------

def test_price_lists_are_fetched_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(estimate, "_cache", {})
    monkeypatch.setattr(estimate, "_gcp_fetch", lambda service: calls.append(service) or GCP_SKUS[service])
    for _ in range(2):  # 요청마다 새 GcpPrices 를 만들어도 카탈로그는 다시 받지 않음
        estimate.GcpPrices().cost(_req(), "asia-northeast3", "gcp")
    assert sorted(calls) == sorted({estimate.GCP_CLOUD_RUN, estimate.GCP_CLOUD_SQL})


def _wait_for(done, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while not done() and time.monotonic() < end:
        time.sleep(0.01)


def test_warm_up_runs_once_and_ignores_failures(monkeypatch):
    monkeypatch.delenv(estimate.WARMUP_ENV, raising=False)
    monkeypatch.setattr(estimate, "_warm_started", False)
    ran = []

    def boom():
        ran.append("boom")
        raise OSError("no network")

    jobs = [boom, lambda: ran.append("ok")]
    assert estimate.warm_up(jobs=jobs) is True
    assert estimate.warm_up(jobs=jobs) is False  # 라우터 시작 훅이 두 번 불려도 한 번만
    _wait_for(lambda: len(ran) == 2)
    assert ran == ["boom", "ok"]  # 앞 작업이 실패해도 다음 작업은 돎


def test_warm_up_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(estimate.WARMUP_ENV, "0")
    monkeypatch.setattr(estimate, "_warm_started", False)
    assert estimate.warm_up(jobs=[lambda: pytest.fail("must not run")]) is False
