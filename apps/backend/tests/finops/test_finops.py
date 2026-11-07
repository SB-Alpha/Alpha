import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.features.finops import ai, service
from app.features.finops.schemas import FinopsInput

FIXTURE = Path(__file__).parent / "fixtures" / "finops_input.sample.json"


def _inp() -> FinopsInput:
    return FinopsInput.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.delenv("ALPHA_AI_ENABLED", raising=False)


def _ai_says(monkeypatch, *notes: str):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    monkeypatch.setattr(ai, "_ask_claude", lambda facts: ai.AiNotes(notes=list(notes)))


def test_savings_are_computed_by_code():
    out = service.run(_inp())
    gcp, azure = out.savings
    assert (gcp.current_monthly_usd, gcp.candidate_monthly_usd) == (60.0, 45.0)
    assert (gcp.monthly_saving_usd, gcp.saving_pct, gcp.complete, gcp.missing) == (15.0, 25.0, True, [])
    # Azure DB 금액이 없으면 합계는 아는 항목만이고, 싸 보이는 가짜 절감을 내지 않음
    assert (azure.candidate_monthly_usd, azure.complete, azure.missing) == (30.0, False, ["azure DB"])
    assert (azure.monthly_saving_usd, azure.saving_pct) == (None, None)
    assert out.best == "gcp"
    assert out.summary_source == "rules"


def test_rule_sentences_use_only_computed_numbers():
    out = service.run(_inp())
    assert any("$15.00" in s and "25.0%" in s for s in out.summary)
    assert any("azure" in n and "azure DB" in n for n in out.notes)
    assert any("azure" in s and "비교할 수 없습니다" in s for s in out.summary)
    assert any("0.5 vCPU" in n for n in out.notes)


def test_candidate_without_a_source_item_is_not_a_complete_quote():
    # 후보 견적에 DB 항목이 통째로 빠지면 금액이 다 있어도 싸 보이는 견적이므로 추천하지 않음
    inp = _inp()
    inp.candidates[0].lines = [line for line in inp.candidates[0].lines if line.item != "DB"]
    gcp = service.run(inp).savings[0]
    assert (gcp.complete, gcp.monthly_saving_usd, gcp.missing) == (False, None, ["gcp DB"])
    assert service.run(inp).best is None


def test_partial_current_cost_is_not_shown_as_the_monthly_cost():
    inp = _inp()
    inp.current.lines[1].monthly_usd = None  # 원본 DB 금액 미제공
    out = service.run(inp)
    assert all(s.current_monthly_usd is None and s.monthly_saving_usd is None for s in out.savings)
    assert "미제공" in out.summary[0] and "아는 항목 합계 $36.00" in out.summary[0]
    assert any("aws DB" in n for n in out.notes)
    assert out.best is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_nan_infinite_or_negative_amounts_are_rejected(bad):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["candidates"][0]["lines"][0]["monthly_usd"] = bad
    with pytest.raises(ValidationError):
        FinopsInput.model_validate(raw)


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


def test_ai_explanation_is_added_after_the_rule_notes(monkeypatch):
    rules = service.run(_inp())
    _ai_says(monkeypatch, "Azure 는 DB 금액이 없어서 전체 비용을 판단하기 어렵습니다.")
    out = service.run(_inp())
    assert out.summary_source == "ai"
    assert out.summary == rules.summary  # 금액·증감·추천 문장은 그대로 코드 문장
    assert out.notes[: len(rules.notes)] == rules.notes  # 규칙 주의점은 지워지지 않음
    assert out.notes[-1].startswith("Azure")
    assert out.best == "gcp" and out.savings[0].monthly_saving_usd == 15.0


@pytest.mark.parametrize("note", [
    "GCP 로 옮기면 월 $60 (60%) 을 아낍니다.",  # 입력에 있는 숫자를 틀리게 조합
    "1년이면 큰 차이입니다.",  # 지어낸 기간
    "Azure 를 추천합니다.",  # 숫자 없는 추천
    "GCP 가 더 저렴한 선택입니다.",
])
def test_ai_notes_with_numbers_or_cost_judgements_are_dropped(monkeypatch, note):
    _ai_says(monkeypatch, note)
    out = service.run(_inp())
    assert out.summary_source == "rules"
    assert note not in out.notes


def test_empty_ai_notes_keep_the_rule_notes(monkeypatch):
    rules = service.run(_inp())
    _ai_says(monkeypatch)  # 설명 없음
    out = service.run(_inp())
    assert (out.summary_source, out.notes) == ("rules", rules.notes)


def test_ai_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")

    def boom(facts):
        raise RuntimeError("down")

    monkeypatch.setattr(ai, "_ask_claude", boom)
    assert service.run(_inp()).summary_source == "rules"


def test_prompt_carries_the_rule_sentences_for_context():
    out = service.run(_inp())
    facts = json.loads(ai._facts_for_prompt(_inp(), out))
    assert facts["rule_summary"] == out.summary and facts["rule_notes"] == out.notes
    assert "run_id" not in facts["input"]


@pytest.mark.parametrize(("path", "bad"), [
    (("candidates", 0, "cloud"), "gcp (월 90% 확정 절감 확인됨)"),  # 결론 문장에 그대로 들어가는 자리
    (("candidates", 0, "cloud"), "GCP"),
    (("candidates", 0, "lines", 0, "item"), "앱 (90% 절감 확정)"),
    (("candidates", 0, "lines", 0, "item"), "$0 앱"),
])
def test_names_that_go_into_the_summary_cannot_carry_sentences(path, bad):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    node = raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = bad
    with pytest.raises(ValidationError):
        FinopsInput.model_validate(raw)


def test_usage_period_warning_only_when_there_is_a_sizing_suggestion():
    inp = _inp()
    inp.period_days, inp.rightsizing = None, []
    assert not any("사용량을 본 기간" in n for n in service.run(inp).notes)
    inp = _inp()
    inp.period_days = None
    assert any("사용량을 본 기간" in n for n in service.run(inp).notes)


@pytest.mark.parametrize("bad", [True, "30", 0])
def test_usage_period_must_be_a_real_positive_integer(bad):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["period_days"] = bad
    with pytest.raises(ValidationError):
        FinopsInput.model_validate(raw)
