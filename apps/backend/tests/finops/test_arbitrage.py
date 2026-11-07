"""동적 트래픽 차익거래 (Dynamic Cost-Arbitrage Routing) 테스트. 실제 네트워크는 쓰지 않습니다.

- 복제 지연(Replication Lag) 세이프가드: 임계치 초과 시 즉시 마스터로 100% 펜싱
- 트래픽 밀도 기반 차익거래: 유휴 시간이 높고 저밀도일 때 Scale-to-Zero Cloud Run 우회로 절감 달성
- 과도한 RPS 시 원본 Fargate 상시 가동 유지
- egress 단가는 가격 API 모양의 가짜 응답에서 받고, 못 받으면 절감·가중치를 내지 않음
- 환율은 USD 1.0, JPY·KRW 는 custom_fx_rate 가 있을 때만 환산
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.features.finops import arbitrage, estimate, router
from app.features.finops.estimate import AwsPrices, GcpPrices, PriceUnavailable
from app.features.finops.schemas import ArbitrageRequest
from app.main import app
from tests.finops.test_estimate import (
    GCP_SKUS,
    TODAY,
    FakePricingClient,
    fake_azure_query,
)

client = TestClient(app)

# -- 가격 API 응답 모양 (2026-11-07 실제 응답에서 필요한 칸만 옮김) -----------------------------------------


def _dt_item(usagetype: str, from_region: str, transfer: str, tiers: list[tuple[str, str, str]]) -> dict:
    dims = {f"R{i}": {"beginRange": begin, "unit": "GB", "pricePerUnit": {"USD": usd}, "description": f"{usd} per GB"}
            for i, (begin, _end, usd) in enumerate(tiers)}
    return {"product": {"productFamily": "Data Transfer", "attributes": {
                "usagetype": usagetype, "fromRegionCode": from_region, "transferType": transfer, "toLocation": "External"}},
            "terms": {"OnDemand": {"T": {"priceDimensions": dims}}}}


AWS_DATA_TRANSFER = [
    _dt_item("Global-DataTransfer-Out-Bytes", "", "AWS Outbound", [("0", "100", "0.0000000000")]),  # 전 세계 합산 무료
    _dt_item("APN2-DataTransfer-Out-Bytes", "ap-northeast-2", "AWS Outbound", [
        ("153600", "Inf", "0.1080000000"), ("0", "10240", "0.1260000000"),  # 순서가 섞여 와도 시작점으로 고름
        ("10240", "51200", "0.1220000000"), ("51200", "153600", "0.1170000000")]),
    _dt_item("APN2-DataTransfer-Out-ABytes", "ap-northeast-2", "Accelerated AWS Outbound from close by location",
             [("0", "Inf", "0.0400000000")]),
]


def fake_bulk(service_code: str, region: str) -> list[dict]:
    assert service_code == "AWSDataTransfer" and region == "ap-northeast-2"
    return AWS_DATA_TRANSFER


def _egress_sku(description: str, regions: list[str], tiers: list[tuple[int, int]]) -> dict:
    rates = [{"startUsageAmount": start, "unitPrice": {"units": "0", "nanos": nanos}} for start, nanos in tiers]
    return {"description": description, "serviceRegions": regions,
            "category": {"usageType": "OnDemand", "resourceGroup": "PremiumInternetEgress"},
            "pricingInfo": [{"pricingExpression": {"tieredRates": rates}}]}


RUN_EGRESS = [
    _egress_sku("Cloud Run Network Internet Data Transfer Out AsiaPacific to AsiaPacific",
                ["asia-east1", "asia-northeast3"], [(0, 120000000), (10240, 85000000), (153600, 80000000)]),
    _egress_sku("Cloud Run Network Internet Data Transfer Out Intercontinental to China", ["global"],
                [(0, 190000000)]),  # 대륙 간 행은 리전 필터에서 빠짐
]
GCP_WITH_EGRESS = {**GCP_SKUS, estimate.GCP_CLOUD_RUN: GCP_SKUS[estimate.GCP_CLOUD_RUN] + RUN_EGRESS}
NET_PER_GB = 0.12 - 0.126


def _prices(gcp_skus=GCP_WITH_EGRESS) -> dict:
    return {"aws": AwsPrices(bulk=fake_bulk), "gcp": GcpPrices(gcp_skus.__getitem__)}


def _calc(prices: dict | None = None, **overrides):
    return arbitrage.calculate_arbitrage(ArbitrageRequest(**overrides), **(prices or {}), today=date(2026, 11, 7))


LOW_TRAFFIC = {"replication_lag_bytes": 0, "rps": 2.0, "idle_percent": 70.0, "read_ratio": 0.8, "app_vcpu": 1.0,
               "app_memory_mib": 2048, "avg_latency_ms": 100.0, "avg_response_kb": 4.0}


# -- egress 단가 조회 --------------------------------------------------------------------------------

def test_egress_lookups_pick_the_first_paid_tier():
    aws = AwsPrices(bulk=fake_bulk).internet_egress("ap-northeast-2")
    gcp = GcpPrices(GCP_WITH_EGRESS.__getitem__).internet_egress("asia-northeast3")
    assert (aws.usd, aws.unit) == (0.126, "GB") and aws.sku.startswith("APN2-DataTransfer-Out-Bytes")
    assert (gcp.usd, gcp.unit) == (0.12, "GiB") and "AsiaPacific to AsiaPacific" in gcp.sku


def test_egress_lookup_without_a_regional_sku_is_not_guessed():
    with pytest.raises(PriceUnavailable):
        GcpPrices(GCP_WITH_EGRESS.__getitem__).internet_egress("europe-west1")
    with pytest.raises(PriceUnavailable):  # 같은 리전에 다른 단가 두 줄
        AwsPrices(bulk=lambda s, r: AWS_DATA_TRANSFER + [_dt_item(
            "APN2-DataTransfer-Out-Bytes", "ap-northeast-2", "AWS Outbound", [("0", "Inf", "0.2")])]
        ).internet_egress("ap-northeast-2")


# -- 판단 ----------------------------------------------------------------------------------------

MEASURED_LAG_B = 6760  # 2026-11-07 실제 RDS→Cloud SQL 이전에서 전환 순간 잰 복제 지연


def test_unknown_lag_is_not_treated_as_safe():
    """지연을 모르면(None) 0 으로 보지 않고 읽기 우회를 하지 않습니다."""
    result = _calc(replication_lag_bytes=None, rps=10.0, idle_percent=50.0)
    assert result.lag_fenced is True
    assert (result.read_weight_target, result.read_weight_source) == (0.0, 1.0)
    assert any(r.startswith("미제공: 복제 지연") for r in result.reasons)
    assert result.recommendation.startswith("미제공: 복제 지연")  # FENCED_PRIMARY(지연 초과)와 구분


def test_measured_lag_from_a_real_migration_is_below_the_threshold():
    """실측 지연(수백 B~수십 KB)은 전환 점검과 같은 1 MiB 기준 아래라 펜싱되지 않습니다."""
    for lag in (152, MEASURED_LAG_B, 56_584):
        assert _calc(replication_lag_bytes=lag, rps=10.0, idle_percent=50.0).lag_fenced is False
    assert _calc(replication_lag_bytes=2 * 1024 * 1024, rps=10.0, idle_percent=50.0).lag_fenced is True


def test_arbitrage_replication_lag_fencing():
    """복제 지연이 임계치(1024 B)를 초과하면 egress 단가와 상관없이 원본으로 펜싱합니다."""
    result = _calc(replication_lag_bytes=2048, max_safe_lag_bytes=1024, rps=10.0, idle_percent=50.0)
    assert result.lag_fenced is True
    assert (result.read_weight_target, result.read_weight_source) == (0.0, 1.0)
    assert result.monthly_savings == 0.0
    assert "FENCED_PRIMARY" in result.recommendation
    assert any("복제 지연" in r for r in result.reasons)


def test_arbitrage_savings_with_known_egress():
    """egress 단가를 받으면 순 송신 단가(GCP - AWS)로 절감을 계산하고 출처를 남깁니다."""
    result = _calc(_prices(), **LOW_TRAFFIC)
    assert result.lag_fenced is False
    assert (result.read_weight_target, result.read_weight_source) == (1.0, 0.0)
    assert result.monthly_savings > 0.0 and result.savings_percent > 0.0
    assert "DYNAMIC_ARBITRAGE" in result.recommendation
    assert result.egress_usd_per_gb == pytest.approx(NET_PER_GB)
    egress_gb = 2.0 * 0.8 * 730 * 3600 * 0.3 * 4.0 / (1024 * 1024)
    assert result.cross_cloud_egress_monthly_usd == round(egress_gb * NET_PER_GB, 2)  # 음수: 송신 요금이 줄어듦
    assert "aws-price-list-bulk-api 2026-11-07 APN2-DataTransfer-Out-Bytes" in result.egress_source
    assert "gcp-cloud-billing-catalog 2026-11-07" in result.egress_source
    assert any("첫 유료 구간" in r for r in result.reasons)


@pytest.mark.parametrize("prices", [None, "lookup-fails"])
def test_unknown_egress_gives_no_savings_and_no_weights(prices):
    """가격 객체가 없거나(네트워크 안 씀) 조회가 실패하면 절감·가중치·교차점을 내지 않습니다."""
    given = _prices(GCP_SKUS) if prices else None  # GCP_SKUS 에는 송신 SKU 가 없음
    result = _calc(given, **LOW_TRAFFIC)
    assert result.recommendation == arbitrage.NO_EGRESS
    assert result.read_weight_target is None and result.read_weight_source is None
    assert result.monthly_savings is None and result.savings_percent is None
    assert result.arbitrage_monthly_cost is None and result.crossover_rps is None
    assert result.cross_cloud_egress_monthly_usd is None and result.egress_usd_per_gb is None
    assert result.egress_source is None
    assert result.baseline_monthly_cost == round(730 * (0.04048 + 2 * 0.004445), 2)  # 원본 비용은 그대로 숫자
    if prices:
        assert any("조회에 실패" in r for r in result.reasons)


def test_arbitrage_high_rps_fargate_retention():
    """RPS가 극단적으로 높으면 Scale-to-Zero 과금이 Fargate 고정비를 넘어 원본 Fargate를 유지합니다."""
    result = _calc(_prices(), replication_lag_bytes=0, rps=5000.0, idle_percent=0.0, read_ratio=0.8,
                   app_vcpu=1.0, app_memory_mib=2048, avg_latency_ms=200.0, avg_response_kb=10.0)
    assert (result.read_weight_target, result.read_weight_source) == (0.0, 1.0)
    assert result.monthly_savings == 0.0
    assert "FENCED_PRIMARY" in result.recommendation


def test_arbitrage_zero_rps_returns_zero_savings():
    """트래픽이 없는 유휴 상태(RPS 0)는 egress 와 상관없이 절감액 0과 IDLE 상태입니다."""
    res = _calc(rps=0.0, idle_percent=70.0)
    assert (res.read_weight_target, res.read_weight_source) == (0.0, 1.0)
    assert res.monthly_savings == 0.0 and res.savings_percent == 0.0
    assert "IDLE" in res.recommendation


def test_arbitrage_crossover_rps_is_independent_of_input_rps():
    """해석적 Crossover RPS는 입력 RPS에 따라 왜곡되지 않고 일관된 변곡점을 제공합니다."""
    res1 = _calc(_prices(), rps=2.0, idle_percent=70.0, avg_latency_ms=100.0)
    res2 = _calc(_prices(), rps=20.0, idle_percent=70.0, avg_latency_ms=100.0)
    assert res1.crossover_rps is not None
    assert res1.crossover_rps == res2.crossover_rps


# -- 환율 -----------------------------------------------------------------------------------------

def test_usd_rate_is_identity_and_ignores_custom_rate():
    res = _calc(_prices(), **LOW_TRAFFIC, currency="USD", custom_fx_rate=1400.0)
    assert res.fx_rate == 1.0 and res.currency == "USD"
    assert res.baseline_monthly_cost == round(730 * (0.04048 + 2 * 0.004445), 2)
    assert any("custom_fx_rate 는 쓰지 않았습니다" in r for r in res.reasons)


def test_jpy_without_custom_rate_has_no_amounts():
    """고정 환율로 채우지 않습니다. 환율이 없으면 환산 금액은 None 입니다."""
    res = _calc(_prices(), **LOW_TRAFFIC, currency="JPY")
    assert res.currency == "JPY" and res.fx_rate is None
    assert res.baseline_monthly_cost is None and res.arbitrage_monthly_cost is None and res.monthly_savings is None
    assert res.savings_percent is not None and res.read_weight_target == 1.0  # 비율·판단은 USD 로 정해짐
    assert any("환율(custom_fx_rate)이 미제공" in r for r in res.reasons)


@pytest.mark.parametrize(("currency", "rate"), [("JPY", 150.0), ("KRW", 1400.0)])
def test_custom_rate_converts_amounts(currency, rate):
    usd = _calc(_prices(), **LOW_TRAFFIC)
    res = _calc(_prices(), **LOW_TRAFFIC, currency=currency, custom_fx_rate=rate)
    assert res.fx_rate == rate
    baseline_usd = 730 * (0.04048 + 2 * 0.004445)
    assert res.baseline_monthly_cost == round(baseline_usd * rate, 0)
    assert res.monthly_savings == pytest.approx(usd.monthly_savings * rate, abs=rate)  # USD 는 센트, 나머지는 정수 반올림


def test_arbitrage_rejects_infinite_custom_fx():
    """custom_fx_rate 에 무한대나 음수, 0이 들어가면 Pydantic ValidationError 로 차단됩니다."""
    for bad in (float("inf"), -1.0, 0.0):
        with pytest.raises(ValidationError):
            ArbitrageRequest(custom_fx_rate=bad)


# -- API ------------------------------------------------------------------------------------------

@pytest.fixture
def fake_prices(monkeypatch):
    regions = []

    class Aws(AwsPrices):
        def internet_egress(self, region):
            regions.append(region)
            return super().internet_egress(region)

    monkeypatch.setattr(router, "_price_sources", lambda: (Aws(bulk=fake_bulk), GcpPrices(GCP_WITH_EGRESS.__getitem__)))
    return regions


def test_arbitrage_router_api(fake_prices):
    """FastAPI POST /finops/arbitrage 는 가격 API 객체를 넘겨 egress 단가를 받습니다."""
    payload = {"source_cloud": "aws", "target_cloud": "gcp", "app_vcpu": 1.0, "app_memory_mib": 2048,
               "app_instances": 1, "rps": 2.0, "read_ratio": 0.8, "idle_percent": 70.0, "currency": "USD",
               "replication_lag_bytes": MEASURED_LAG_B}
    resp = client.post("/finops/arbitrage", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_master"] == "SOURCE" and data["write_destination"] == "SOURCE"
    assert data["read_weight_target"] == 1.0
    assert data["egress_usd_per_gb"] == pytest.approx(NET_PER_GB) and data["egress_source"]
    assert fake_prices == ["ap-northeast-2"]


def test_estimate_includes_arbitrage_when_traffic_present(monkeypatch, fake_prices):
    """POST /finops/estimate 호출 시 traffic 정보가 있으면 arbitrage 분석 결과가 포함됩니다."""
    real_build = estimate.build_input
    monkeypatch.setattr(estimate, "build_input", lambda req: real_build(
        req, aws=AwsPrices(FakePricingClient()), gcp=GcpPrices(GCP_SKUS.__getitem__),
        azure=estimate.AzurePrices(fake_azure_query), today=TODAY))
    payload = {"source_region": "ap-northeast-2", "app_vcpu": 1.0, "app_memory_mib": 2048, "app_instances": 1,
               "candidates": [{"cloud": "gcp", "region": "asia-northeast3"}],
               "traffic": {"idle_percent": 70.0, "rps": 2.0, "avg_latency_ms": 100.0, "read_ratio": 0.8, "avg_response_kb": 4.0,
                           "replication_lag_bytes": MEASURED_LAG_B}}
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    arb = resp.json()["arbitrage"]
    assert arb is not None
    assert arb["read_weight_target"] == 1.0
    assert arb["write_destination"] == "SOURCE"
    assert "AsiaPacific" in arb["egress_source"]


def test_estimate_with_an_odd_region_reports_egress_as_not_provided(monkeypatch, fake_prices):
    """리전 이름 모양이 맞지 않아도 500 이 아니라 egress "미제공" 입니다 (필터에 넣기 전에 막힘)."""
    real_build = estimate.build_input
    monkeypatch.setattr(estimate, "build_input", lambda req: real_build(
        req, aws=AwsPrices(FakePricingClient()), gcp=GcpPrices(GCP_SKUS.__getitem__),
        azure=estimate.AzurePrices(fake_azure_query), today=TODAY))
    payload = {"source_region": "AP-NORTHEAST-2", "app_vcpu": 1.0, "app_memory_mib": 2048,
               "candidates": [{"cloud": "gcp", "region": "asia-northeast3"}],
               "traffic": {"idle_percent": 70.0, "rps": 2.0, "avg_latency_ms": 100.0, "read_ratio": 0.8, "avg_response_kb": 4.0,
                           "replication_lag_bytes": MEASURED_LAG_B}}
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    arb = resp.json()["arbitrage"]
    assert arb["recommendation"] == arbitrage.NO_EGRESS and arb["monthly_savings"] is None
    assert any("ValueError" in r for r in arb["reasons"])


def test_estimate_without_lag_fences_instead_of_assuming_zero(monkeypatch, fake_prices):
    """/finops/estimate 에 복제 지연이 없으면 0(안전)으로 바꾸지 않고 미제공으로 펜싱합니다."""
    real_build = estimate.build_input
    monkeypatch.setattr(estimate, "build_input", lambda req: real_build(
        req, aws=AwsPrices(FakePricingClient()), gcp=GcpPrices(GCP_SKUS.__getitem__),
        azure=estimate.AzurePrices(fake_azure_query), today=TODAY))
    payload = {"source_region": "ap-northeast-2", "app_vcpu": 1.0, "app_memory_mib": 2048, "app_instances": 1,
               "candidates": [{"cloud": "gcp", "region": "asia-northeast3"}],
               "traffic": {"idle_percent": 70.0, "rps": 2.0, "avg_latency_ms": 100.0, "read_ratio": 0.8, "avg_response_kb": 4.0}}
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    arb = resp.json()["arbitrage"]
    assert arb["lag_fenced"] is True and arb["read_weight_target"] == 0.0
    assert any(r.startswith("미제공: 복제 지연") for r in arb["reasons"])


def test_estimate_without_a_gcp_candidate_has_no_arbitrage(monkeypatch, fake_prices):
    """읽기 분산 대상(GCP 후보)이 없으면 목적지 리전을 짐작하지 않고 분석을 내지 않습니다."""
    real_build = estimate.build_input
    monkeypatch.setattr(estimate, "build_input", lambda req: real_build(
        req, aws=AwsPrices(FakePricingClient()), gcp=GcpPrices(GCP_SKUS.__getitem__),
        azure=estimate.AzurePrices(fake_azure_query), today=TODAY))
    payload = {"source_region": "ap-northeast-2", "app_vcpu": 1.0, "app_memory_mib": 2048,
               "candidates": [{"cloud": "azure", "region": "koreacentral"}],
               "traffic": {"idle_percent": 70.0, "rps": 2.0, "avg_latency_ms": 100.0, "read_ratio": 0.8, "avg_response_kb": 4.0,
                           "replication_lag_bytes": MEASURED_LAG_B}}
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    assert resp.json()["arbitrage"] is None
    assert fake_prices == []  # egress 단가도 조회하지 않음


def test_egress_skus_do_not_disturb_cloud_run_compute_prices():
    """같은 Cloud Run 카탈로그에 송신 SKU 가 섞여도 CPU·메모리 단가는 그대로 한 건씩 고릅니다."""
    one = GcpPrices(GCP_WITH_EGRESS.__getitem__).app_instance_month(
        estimate.CostRequest(source_region="ap-northeast-2", app_vcpu=1, app_memory_mib=2048), "asia-northeast3")
    assert one == pytest.approx(730 * 3600 * (0.0000216 + 2 * 0.0000024))


# -- 복제 지연: 모르면 None 그대로, 기준은 전환 승인 ② 와 같은 값 ------------------------------------------

def test_lag_threshold_matches_cutover_readiness():
    """차익거래 펜싱 기준과 전환 승인 ② 의 복제 지연 기준이 같아야 화면끼리 판단이 어긋나지 않습니다."""
    from app.features.finops.schemas import MAX_SAFE_LAG_BYTES, Traffic
    from app.features.workflows.readiness import MAX_REPLICATION_LAG_BYTES

    assert MAX_SAFE_LAG_BYTES == MAX_REPLICATION_LAG_BYTES
    assert ArbitrageRequest().max_safe_lag_bytes == MAX_SAFE_LAG_BYTES
    assert Traffic(idle_percent=0).max_safe_lag_bytes == MAX_SAFE_LAG_BYTES


def test_unknown_lag_is_not_treated_as_zero():
    """지연을 모르면(None, 기본값) 원본에 고정하고, 잰 값 0 은 안전으로 보고 읽기 우회를 계산합니다."""
    assert ArbitrageRequest().replication_lag_bytes is None
    unknown = _calc(_prices(), **{**LOW_TRAFFIC, "replication_lag_bytes": None})
    assert unknown.lag_fenced is True and unknown.read_weight_target == 0.0
    measured_zero = _calc(_prices(), **LOW_TRAFFIC)
    assert measured_zero.lag_fenced is False and measured_zero.read_weight_target == 1.0
    assert not any(r.startswith("미제공: 복제 지연") for r in measured_zero.reasons)


def _estimate_with_lag(monkeypatch, lag: int | None) -> dict:
    real_build = estimate.build_input
    monkeypatch.setattr(estimate, "build_input", lambda req: real_build(
        req, aws=AwsPrices(FakePricingClient()), gcp=GcpPrices(GCP_SKUS.__getitem__),
        azure=estimate.AzurePrices(fake_azure_query), today=TODAY))
    traffic = {"idle_percent": 70.0, "rps": 2.0, "avg_latency_ms": 100.0, "read_ratio": 0.8, "avg_response_kb": 4.0}
    if lag is not None:
        traffic["replication_lag_bytes"] = lag
    payload = {"source_region": "ap-northeast-2", "app_vcpu": 1.0, "app_memory_mib": 2048, "app_instances": 1,
               "candidates": [{"cloud": "gcp", "region": "asia-northeast3"}], "traffic": traffic}
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    return resp.json()["arbitrage"]


def test_estimate_passes_unknown_lag_as_none(monkeypatch, fake_prices):
    """/estimate 는 traffic 에 지연이 없으면 0 으로 바꾸지 않고 None 으로 넘겨 원본에 고정합니다."""
    arb = _estimate_with_lag(monkeypatch, None)
    assert arb["lag_fenced"] is True and arb["read_weight_target"] == 0.0
    assert arb["recommendation"].startswith("미제공: 복제 지연")


@pytest.mark.parametrize(("lag", "fenced"), [(6_760, False), (1024 * 1024 + 1, True)])
def test_estimate_fences_on_measured_lag(monkeypatch, fake_prices, lag, fenced):
    """실측 지연(예: 전환 순간 6,760 B)은 1 MiB 기준 아래라 펜싱하지 않고, 기준을 넘으면 펜싱합니다."""
    arb = _estimate_with_lag(monkeypatch, lag)
    assert arb["lag_fenced"] is fenced
    assert not any(r.startswith("미제공: 복제 지연") for r in arb["reasons"])
