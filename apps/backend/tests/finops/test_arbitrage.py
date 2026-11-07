"""동적 트래픽 차익거래 (Dynamic Cost-Arbitrage Routing) 테스트.

- 복제 지연(Replication Lag) 세이프가드: 임계치 초과 시 즉시 마스터로 100% 펜싱
- 트래픽 밀도 기반 차익거래: 유휴 시간이 높고 저밀도일 때 Scale-to-Zero Cloud Run 우회로 절감 달성
- 과도한 RPS 및 Egress 부담 시 원본 Fargate 상시 가동 유지
- 통화 환율(USD, JPY, KRW) 변동성 대응 연산 검증
"""

import pytest
from fastapi.testclient import TestClient

from pydantic import ValidationError

from app.features.finops import arbitrage
from app.features.finops.schemas import ArbitrageRequest, CostRequest, Traffic
from app.main import app

client = TestClient(app)


def test_arbitrage_replication_lag_fencing():
    """복제 지연이 임계치(1024 B)를 초과하면 데이터 정합성을 위해 즉시 읽기 우회를 차단하고 원본으로 펜싱합니다."""
    req = ArbitrageRequest(
        replication_lag_bytes=2048,
        max_safe_lag_bytes=1024,
        rps=10.0,
        idle_percent=50.0,
        read_ratio=0.8,
    )
    result = arbitrage.calculate_arbitrage(req)
    assert result.lag_fenced is True
    assert result.read_weight_target == 0.0
    assert result.read_weight_source == 1.0
    assert result.monthly_savings == 0.0
    assert "FENCED_PRIMARY" in result.recommendation
    assert any("복제 지연" in r for r in result.reasons)


def test_arbitrage_savings_with_cloud_run_scale_to_zero():
    """유휴 시간이 높고 간헐적/야간 트래픽(RPS 2)인 경우 Cloud Run Scale-to-Zero로 큰 절감이 발생합니다."""
    req = ArbitrageRequest(
        replication_lag_bytes=0,
        max_safe_lag_bytes=1024,
        rps=2.0,
        idle_percent=70.0,
        read_ratio=0.8,
        app_vcpu=1.0,
        app_memory_mib=2048,
        avg_latency_ms=100.0,
        avg_response_kb=4.0,
        currency="USD",
    )
    result = arbitrage.calculate_arbitrage(req)
    assert result.lag_fenced is False
    assert result.read_weight_target == 1.0
    assert result.read_weight_source == 0.0
    assert result.monthly_savings > 0.0
    assert result.savings_percent > 0.0
    assert "DYNAMIC_ARBITRAGE" in result.recommendation
    assert result.cross_cloud_egress_monthly_usd > 0.0


def test_arbitrage_high_rps_fargate_retention():
    """RPS가 극단적으로 높으면 Scale-to-Zero 과금 및 Egress 비용이 Fargate 고정비를 초과하여 원본 Fargate를 유지합니다."""
    req = ArbitrageRequest(
        replication_lag_bytes=0,
        max_safe_lag_bytes=1024,
        rps=5000.0,  # 매우 높은 RPS
        idle_percent=0.0,  # 내내 트래픽 집중
        read_ratio=0.8,
        app_vcpu=1.0,
        app_memory_mib=2048,
        avg_latency_ms=200.0,
        avg_response_kb=10.0,
        currency="USD",
    )
    result = arbitrage.calculate_arbitrage(req)
    assert result.read_weight_target == 0.0
    assert result.read_weight_source == 1.0
    assert result.monthly_savings == 0.0
    assert "FENCED_PRIMARY" in result.recommendation


def test_arbitrage_fx_currency_conversion():
    """엔화(JPY), 원화(KRW) 환율 적용 시 반올림 및 금액 변환이 정상적으로 수행됩니다."""
    req_usd = ArbitrageRequest(
        rps=15.0,
        idle_percent=50.0,
        currency="USD",
    )
    res_usd = arbitrage.calculate_arbitrage(req_usd)

    req_jpy = ArbitrageRequest(
        rps=15.0,
        idle_percent=50.0,
        currency="JPY",
    )
    res_jpy = arbitrage.calculate_arbitrage(req_jpy)
    assert res_jpy.currency == "JPY"
    assert res_jpy.fx_rate == 154.5
    # JPY는 정수 반올림
    assert res_jpy.baseline_monthly_cost == round(res_usd.baseline_monthly_cost * 154.5, 0)

    req_krw = ArbitrageRequest(
        rps=15.0,
        idle_percent=50.0,
        currency="KRW",
        custom_fx_rate=1400.0,
    )
    res_krw = arbitrage.calculate_arbitrage(req_krw)
    assert res_krw.currency == "KRW"
    assert res_krw.fx_rate == 1400.0
    assert res_krw.baseline_monthly_cost == round(res_usd.baseline_monthly_cost * 1400.0, 0)


def test_arbitrage_router_api():
    """FastAPI POST /finops/arbitrage 엔드포인트 호출 검증."""
    payload = {
        "source_cloud": "aws",
        "target_cloud": "gcp",
        "app_vcpu": 1.0,
        "app_memory_mib": 2048,
        "app_instances": 1,
        "rps": 2.0,
        "read_ratio": 0.8,
        "idle_percent": 70.0,
        "currency": "USD",
    }
    resp = client.post("/finops/arbitrage", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "read_weight_target" in data
    assert "active_master" in data
    assert data["active_master"] == "SOURCE"
    assert data["write_destination"] == "SOURCE"


def test_estimate_includes_arbitrage_when_traffic_present():
    """POST /finops/estimate 호출 시 traffic 정보가 있으면 arbitrage 분석 결과가 포함됩니다."""
    payload = {
        "source_region": "ap-northeast-2",
        "app_vcpu": 1.0,
        "app_memory_mib": 2048,
        "app_instances": 1,
        "candidates": [{"cloud": "gcp", "region": "asia-northeast3"}],
        "traffic": {
            "idle_percent": 70.0,
            "rps": 2.0,
            "avg_latency_ms": 100.0,
        },
    }
    resp = client.post("/finops/estimate", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "arbitrage" in data
    assert data["arbitrage"] is not None
    assert data["arbitrage"]["read_weight_target"] == 1.0
    assert data["arbitrage"]["write_destination"] == "SOURCE"


def test_arbitrage_zero_rps_returns_zero_savings():
    """트래픽이 없는 유휴 상태(RPS 0)에서는 절감액 0과 IDLE 상태를 반환합니다."""
    req = ArbitrageRequest(rps=0.0, idle_percent=70.0)
    res = arbitrage.calculate_arbitrage(req)
    assert res.read_weight_target == 0.0
    assert res.read_weight_source == 1.0
    assert res.monthly_savings == 0.0
    assert res.savings_percent == 0.0
    assert "IDLE" in res.recommendation


def test_arbitrage_crossover_rps_is_independent_of_input_rps():
    """해석적 Crossover RPS는 입력 RPS에 따라 왜곡되지 않고 일관된 변곡점을 제공합니다."""
    res1 = arbitrage.calculate_arbitrage(ArbitrageRequest(rps=2.0, idle_percent=70.0, avg_latency_ms=100.0))
    res2 = arbitrage.calculate_arbitrage(ArbitrageRequest(rps=20.0, idle_percent=70.0, avg_latency_ms=100.0))
    assert res1.crossover_rps is not None
    assert res1.crossover_rps == res2.crossover_rps


def test_arbitrage_rejects_infinite_custom_fx():
    """custom_fx_rate 에 무한대나 음수, 0이 들어가면 Pydantic ValidationError 로 차단됩니다."""
    with pytest.raises(ValidationError):
        ArbitrageRequest(custom_fx_rate=float("inf"))
    with pytest.raises(ValidationError):
        ArbitrageRequest(custom_fx_rate=-1.0)
    with pytest.raises(ValidationError):
        ArbitrageRequest(custom_fx_rate=0.0)

