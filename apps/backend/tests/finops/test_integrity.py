"""입력 검사와 합계 안전성. 잘못된 입력은 422 로 거절하고, 계산이 넘치면 0 이 아니라 "미제공" 입니다."""

import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.features.finops import estimate, service
from app.features.finops.schemas import (
    Candidate,
    CloudCost,
    CostLine,
    CostRequest,
    FinopsInput,
    Rightsizing,
)
from app.main import create_app


def _line(item: str, usd: float | None) -> CostLine:
    return CostLine(item=item, monthly_usd=usd, source="manual")


def _cost(cloud: str, *lines: CostLine) -> CloudCost:
    return CloudCost(cloud=cloud, lines=list(lines))


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)


# -- 1. 같은 항목을 두 번 넣으면 원본 합계가 부풀어 가짜 절감이 생깁니다 ------------------------------


def test_the_same_item_twice_is_refused():
    with pytest.raises(ValidationError, match="같은 항목"):
        _cost("aws", _line("앱", 50), _line("앱 ", 50))


def test_distinct_items_are_still_accepted():
    assert len(_cost("aws", _line("앱", 50), _line("DB", 10)).lines) == 2


# -- 2. 합계가 넘치면 inf·nan 대신 미제공 ------------------------------------------------------------


def test_an_overflowing_total_is_not_provided():
    huge = _cost("aws", _line("앱", 1e308), _line("DB", 1e308))
    assert service.total(huge) == (None, False)
    assert service.complete_total(huge) is None


def test_an_overflowing_saving_has_no_amount_and_no_nan_in_the_summary():
    inp = FinopsInput(current=_cost("aws", _line("앱", 1e308), _line("DB", 1e308)),
                      candidates=[_cost("gcp", _line("앱", 1), _line("DB", 1))])
    out = service.run(inp)
    gcp = out.savings[0]
    assert (gcp.monthly_saving_usd, gcp.saving_pct, gcp.complete) == (None, None, False)
    text = " ".join(out.summary + out.notes)
    assert "inf" not in text and "nan" not in text
    assert out.best is None


# -- 3. 후보 클라우드는 원본과 다르고 서로 겹치지 않아야 합니다 --------------------------------------


def test_a_candidate_equal_to_the_source_is_refused():
    with pytest.raises(ValidationError, match="원본"):
        FinopsInput(current=_cost("aws", _line("앱", 60)), candidates=[_cost("aws", _line("앱", 30))])


def test_duplicate_candidates_are_refused():
    with pytest.raises(ValidationError, match="두 번"):
        FinopsInput(current=_cost("aws", _line("앱", 60)),
                    candidates=[_cost("gcp", _line("앱", 30)), _cost("gcp", _line("앱", 20))])


def test_estimate_candidates_must_be_unique_and_not_the_aws_source():
    with pytest.raises(ValidationError, match="원본"):
        CostRequest(source_region="ap-northeast-2", candidates=[Candidate(cloud="aws", region="ap-northeast-2")])
    with pytest.raises(ValidationError, match="두 번"):
        CostRequest(source_region="ap-northeast-2", candidates=[Candidate(cloud="gcp", region="asia-northeast3"),
                                                                Candidate(cloud="gcp", region="us-central1")])


# -- 4. 크기는 양수, 상한 안. 모르면 None 그대로 ---------------------------------------------------


@pytest.mark.parametrize("bad", [
    {"app_vcpu": 0}, {"app_vcpu": -1}, {"app_vcpu": float("inf")}, {"app_vcpu": 1e308},
    {"app_memory_mib": 0}, {"app_memory_mib": -2048}, {"app_memory_mib": 10**12},
    {"app_instances": 0}, {"app_instances": 10**9},
    {"db_vcpu": 0}, {"db_memory_gib": -1}, {"db_memory_gib": float("nan")},
    {"db_storage_gb": 0}, {"db_storage_gb": -5}, {"db_storage_gb": 10**12},
])
def test_impossible_sizes_are_refused(bad):
    with pytest.raises(ValidationError):
        CostRequest(source_region="ap-northeast-2", **bad)


def test_unknown_sizes_stay_none():
    req = CostRequest(source_region="ap-northeast-2")
    assert (req.app_vcpu, req.app_memory_mib, req.db_vcpu, req.db_memory_gib, req.db_storage_gb) == (None,) * 5


# -- 5. 크기 제안 문장도 AI 설명과 같은 판단 금지어 검사를 거칩니다 ---------------------------------


@pytest.mark.parametrize("field", ["resource", "current", "suggested", "evidence"])
def test_rightsizing_text_cannot_carry_a_cost_judgement(field):
    values = {"resource": "앱 CPU", "current": "1 vCPU", "suggested": "0.5 vCPU", "evidence": "14일 CPU p95 12%"}
    values[field] = "gcp 로 옮기길 추천합니다"
    with pytest.raises(ValidationError, match="판단"):
        Rightsizing(**values)


def test_rightsizing_evidence_may_carry_measured_numbers():
    r = Rightsizing(resource="앱 CPU", current="1 vCPU", suggested="0.5 vCPU", evidence="14일 CPU 사용률 p95 12%")
    assert r.evidence.endswith("12%")


# -- 라우터: 잘못된 입력은 500 이 아니라 422 -----------------------------------------------------------


def test_routes_answer_422_for_bad_input(monkeypatch):
    def never(_req):  # 검사에 걸리면 가격 API 까지 가지 않아야 합니다
        raise AssertionError("build_input must not run")

    monkeypatch.setattr(estimate, "build_input", never)
    client = TestClient(create_app())
    dup = {"current": {"cloud": "aws", "lines": [{"item": "앱", "monthly_usd": 50, "source": "m"},
                                                  {"item": "앱", "monthly_usd": 50, "source": "m"}]},
           "candidates": []}
    assert client.post("/finops/report", json=dup).status_code == 422
    same = {"current": {"cloud": "aws", "lines": []}, "candidates": [{"cloud": "aws", "lines": []}]}
    assert client.post("/finops/report", json=same).status_code == 422
    for body in ({"source_region": "ap-northeast-2", "app_vcpu": -1},
                 {"source_region": "ap-northeast-2", "db_storage_gb": 10**12},
                 {"source_region": "ap-northeast-2", "candidates": [{"cloud": "aws", "region": "ap-northeast-2"}]}):
        assert client.post("/finops/estimate", json=body).status_code == 422


# -- 6. GCP 토큰 갱신이 requests 없이도 됩니다 -----------------------------------------------------------


def test_gcp_adc_refresh_uses_the_installed_urllib3_transport(monkeypatch):
    monkeypatch.delenv(estimate.GCP_KEY_ENV, raising=False)
    seen = {}

    class Creds:
        token = "tok"

        def refresh(self, request):
            seen["request"] = type(request).__module__

    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes: (Creds(), "proj"))
    params, headers = estimate._gcp_auth_header()
    assert (params, headers) == ({}, {"Authorization": "Bearer tok"})
    assert seen["request"] == "google.auth.transport.urllib3"


def test_finite_guard_never_turns_missing_into_zero():
    assert service.finite_or_none(None) is None
    assert service.finite_or_none(math.inf) is None
    assert service.finite_or_none(0.0) == 0.0
