import json
from pathlib import Path

import pytest

from app.features.finops import ai, service
from app.features.finops.schemas import FinopsInput

FIXTURE = Path(__file__).parent / "fixtures" / "finops_input.sample.json"


def _inp() -> FinopsInput:
    return FinopsInput.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)


def test_savings_are_computed_by_code():
    out = service.run(_inp())
    gcp, azure = out.savings
    assert (gcp.current_monthly_usd, gcp.candidate_monthly_usd) == (60.0, 45.0)
    assert (gcp.monthly_saving_usd, gcp.saving_pct, gcp.complete) == (15.0, 25.0, True)
    # Azure DB 금액이 없으면 합계는 아는 항목만이고, 싸 보이는 가짜 절감을 내지 않음
    assert (azure.candidate_monthly_usd, azure.complete) == (30.0, False)
    assert (azure.monthly_saving_usd, azure.saving_pct) == (None, None)
    assert out.best == "gcp"
    assert out.summary_source == "rules"


def test_rule_sentences_use_only_computed_numbers():
    out = service.run(_inp())
    assert any("$15.00" in s and "25.0%" in s for s in out.summary)
    assert any("일부 항목" in n and "azure" in n for n in out.notes)
    assert any("azure" in s and "비교할 수 없습니다" in s for s in out.summary)
    assert any("0.5 vCPU" in n for n in out.notes)
    facts = ai._facts_for_prompt(_inp(), out)
    assert ai.unknown_numbers(out.summary + out.notes, facts) == []


def test_cost_increase_and_unknown_totals_are_not_recommended():
    inp = _inp()
    inp.candidates[0].lines[0].monthly_usd = 80.0  # gcp 105 > 60
    out = service.run(inp)
    assert out.savings[0].monthly_saving_usd == -45.0
    assert out.best is None
    assert any("늘어날" in s for s in out.summary)


def test_no_amounts_at_all_gives_none_not_zero():
    inp = FinopsInput(current={"cloud": "aws", "lines": [{"item": "앱", "source": "manual"}]},
                      candidates=[{"cloud": "gcp", "lines": []}])
    s = service.run(inp).savings[0]
    assert (s.current_monthly_usd, s.candidate_monthly_usd, s.monthly_saving_usd, s.saving_pct) == (None,) * 4


def test_ai_text_is_used_when_every_number_comes_from_the_input(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setattr(ai, "_ask_claude", lambda facts: ai.AiNotes(
        summary=["현재 AWS 월 비용은 $60 입니다.", "GCP 로 옮기면 월 $15 (25%) 아낄 수 있습니다."],
        notes=["Azure 는 DB 금액이 미제공입니다."],
    ))
    out = service.run(_inp())
    assert out.summary_source == "ai"
    assert out.summary[1].startswith("GCP")
    assert out.best == "gcp" and out.savings[0].monthly_saving_usd == 15.0  # 숫자 결과는 그대로


def test_ai_text_with_an_invented_number_is_dropped(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setattr(ai, "_ask_claude", lambda facts: ai.AiNotes(
        summary=["현재 AWS 월 비용은 $60 입니다.", "GCP 로 옮기면 1년에 $180 을 아낍니다."],  # 180 은 입력에 없음
    ))
    out = service.run(_inp())
    assert out.summary_source == "rules"


def test_ai_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")

    def boom(facts):
        raise RuntimeError("down")

    monkeypatch.setattr(ai, "_ask_claude", boom)
    assert service.run(_inp()).summary_source == "rules"


def test_unknown_numbers_accepts_rounding_and_thousands_separators():
    facts = json.dumps({"a": 1234.567, "b": 25.0})
    assert ai.unknown_numbers(["$1,234.57 와 25% 와 1235"], facts) == []
    assert ai.unknown_numbers(["$999"], facts) == ["999"]


def test_ai_polarity_reversal_is_dropped(monkeypatch):
    """비용 증가인데 AI가 절감이라고 서술하면 규칙 결과로 돌아갑니다."""
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    inp = _inp()
    inp.candidates[0].lines[0].monthly_usd = 80.0  # GCP 비용 105 > AWS 60 (월 $45 증가)
    monkeypatch.setattr(ai, "_ask_claude", lambda facts: ai.AiNotes(
        summary=["현재 AWS 월 비용은 $60 입니다.", "GCP 로 옮기면 월 $45 절감됩니다."],  # 45는 있지만 증감 방향 반대!
        notes=["비용 주의"],
    ))
    out = service.run(inp)
    assert out.summary_source == "rules"  # AI 문장이 기각되고 규칙 문장으로 회귀
    assert any("늘어날" in s for s in out.summary)


def test_finops_api_endpoint():
    """FastAPI main.py 라우터에 정상 등록되어 /finops/report 로 요청이 처리되는지 검증합니다."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = client.post("/finops/report", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["best"] == "gcp"
    assert len(data["savings"]) == 2
    assert data["summary_source"] == "rules"
