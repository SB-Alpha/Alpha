"""교차점: Cloud Run(요청 없으면 0대)이 Fargate(내내 켜짐)보다 싸지는 지점을 코드가 계산합니다.

단가는 test_estimate 의 가짜 가격표입니다 (Fargate vCPU $0.04656/시간 · GB $0.00511/시간,
Cloud Run vCPU $0.0000216/초 · GiB $0.0000024/초). 앱 1 vCPU · 2 GiB, 인스턴스 1개.
"""

import math

import pytest
from pydantic import ValidationError

from app.features.finops import estimate, service
from app.features.finops.estimate import AwsPrices, AzurePrices, Traffic
from tests.finops.test_estimate import GCP_SKUS, FakePricingClient, _req, fake_azure_query

FARGATE = 730 * (1 * 0.04656 + 2 * 0.00511)  # 내내 켜진 Fargate 앱 월 비용
CLOUD_RUN_FULL = 730 * 3600 * (1 * 0.0000216 + 2 * 0.0000024)  # Cloud Run 1대를 한 달 내내 켰을 때


def _build(**overrides):
    return estimate.build_input(_req(**overrides), aws=AwsPrices(FakePricingClient()),
                                gcp=estimate.GcpPrices(GCP_SKUS.__getitem__), azure=AzurePrices(fake_azure_query))


def test_break_even_idle_percent_comes_from_the_two_unit_prices():
    x = _build().crossover
    expected = round((1 - FARGATE / CLOUD_RUN_FULL) * 100, 1)
    assert x.break_even_idle_percent == expected == 40.3
    assert (x.fargate_app_monthly_usd, x.cloud_run_instance_monthly_usd) == (round(FARGATE, 2), round(CLOUD_RUN_FULL, 2))


def test_break_even_rps_counts_cloud_run_instances_from_latency_and_concurrency():
    # 유휴 50% → Cloud Run 1대 비용 = 전체의 절반. 2대가 되면 Fargate 보다 비쌉니다.
    x = _build(traffic=Traffic(idle_percent=50, rps=10, avg_latency_ms=200)).crossover
    share = 0.5
    max_cheaper = math.ceil(FARGATE / (CLOUD_RUN_FULL * share)) - 1
    assert max_cheaper == 1
    assert x.break_even_rps == max_cheaper * 80 / 0.2 == 400
    assert (x.concurrency, x.max_instances, x.reason) == (80, 2, None)


def test_no_rps_crossover_when_one_instance_already_costs_more():
    x = _build(traffic=Traffic(idle_percent=10, rps=10, avg_latency_ms=200)).crossover
    assert x.break_even_rps is None
    assert "1대" in x.reason


def test_no_rps_crossover_inside_the_terraform_instance_cap():
    # 유휴 90% 면 다섯 대까지 싸지만 Terraform 이 max_instance_count = 2 로 막습니다. 넘겨 짐작하지 않습니다.
    x = _build(traffic=Traffic(idle_percent=90, rps=10, avg_latency_ms=200)).crossover
    assert x.break_even_rps is None
    assert "최대 2대" in x.reason and "800" in x.reason


def test_without_latency_the_rps_crossover_is_not_provided():
    x = _build(traffic=Traffic(idle_percent=50, rps=10)).crossover
    assert x.break_even_rps is None and "처리 시간" in x.reason
    assert x.break_even_idle_percent == 40.3  # 단가만으로 나오는 값은 그대로


def test_crossover_is_written_into_the_report_notes():
    inp = _build(traffic=Traffic(idle_percent=50, rps=10, avg_latency_ms=200))
    notes = service.run(inp).notes
    assert any("유휴 40.3%" in n for n in notes)
    assert any("400 RPS" in n for n in notes)


def test_without_a_gcp_candidate_there_is_no_crossover():
    from app.features.finops.schemas import Candidate

    inp = _build(candidates=[Candidate(cloud="azure", region="koreacentral")])
    assert inp.crossover is None


def test_a_price_lookup_failure_leaves_the_crossover_not_provided():
    class Broken(estimate.GcpPrices):
        def _pick(self, *a, **k):
            raise estimate.PriceUnavailable("no sku")

    inp = estimate.build_input(_req(), aws=AwsPrices(FakePricingClient()), gcp=Broken(GCP_SKUS.__getitem__),
                               azure=AzurePrices(fake_azure_query))
    x = inp.crossover
    assert (x.break_even_idle_percent, x.break_even_rps) == (None, None)
    assert x.reason


@pytest.mark.parametrize("bad", [0, -5, float("inf"), 120_000])
def test_impossible_latency_is_refused(bad):
    with pytest.raises(ValidationError):
        Traffic(idle_percent=10, avg_latency_ms=bad)
