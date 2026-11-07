"""실패한 단계의 측정값을 AI 입력으로 줄이는 규칙과, 실제로 요청에 실리는지 확인합니다 (Claude 는 부르지 않음)."""

import json
import sys
from types import ModuleType, SimpleNamespace

from app.features.workflows.healer import AiDiagnosis, _failed_stage_facts, diagnose_run

from .test_healer import _sample_run
from app.features.workflows.schemas import StageName


def _with_metrics(metrics, stage=StageName.CUTOVER, message="전환 실패"):
    run = _sample_run(stage=stage, message=message)
    stages = [s.model_copy(update={"metrics": metrics, "progress": 70}) if s.stage == stage else s for s in run.stages]
    return run.model_copy(update={"stages": stages})


def test_allowed_numbers_and_short_words_are_kept():
    facts = _failed_stage_facts(_with_metrics({
        "phase": "streaming", "replication_lag_bytes": 4096, "verification": "mismatch", "mismatch": 3,
        "cutover_sequence": "INCOMPLETE", "cutover_sequence_missing": ["sink_stop", "sequence_sync"],
    }), StageName.CUTOVER)
    assert facts == {
        "phase": "streaming", "replication_lag_bytes": 4096, "verification": "mismatch", "mismatch": 3,
        "cutover_sequence": "INCOMPLETE", "cutover_sequence_missing": ["sink_stop", "sequence_sync"],
        "stage_progress": 70,
    }


def test_unknown_keys_and_unsafe_values_are_dropped():
    facts = _failed_stage_facts(_with_metrics({
        "phase": "postgres://u:pw@db.internal:5432/app",  # 주소·접속 정보가 든 문자열
        "verification": "match with spaces and 한글",  # 짧은 단어가 아님
        "measure_error": "boom",  # 허용 키가 아님
        "serving": "gcp",  # 허용 키가 아님
        "replication_lag_bytes": {"nested": 1},  # 숫자가 아님
        "cutover_sequence_missing": [{"a": 1}, "ok_step", "x" * 41],
    }), StageName.CUTOVER)
    assert facts == {"cutover_sequence_missing": ["ok_step"], "stage_progress": 70}


def test_no_metrics_means_no_facts_key():
    assert _failed_stage_facts(_with_metrics({}), StageName.CUTOVER) == {}


def test_cutover_steps_keep_only_safe_fields_and_scrubbed_errors():
    facts = _failed_stage_facts(_with_metrics({"cutover_steps": [
        {"step": "hold", "elapsed_ms": 120, "waited_ms": 5, "extra": "버림"},
        {"step": "snapshot_compare", "mismatch": 2, "error": "connect postgres://u:secret123@10.0.0.5:5432/app failed"},
        {"step": "bad step name!", "elapsed_ms": 1},  # 이름이 안전하지 않으면 통째로 버림
        {"no_step": True},
        "문자열",
    ]}), StageName.CUTOVER)
    steps = facts["cutover_steps"]
    assert [s["step"] for s in steps] == ["hold", "snapshot_compare"]
    assert steps[0] == {"step": "hold", "elapsed_ms": 120, "waited_ms": 5}
    err = steps[1]["error"]
    assert "secret123" not in err and "10.0.0.5" not in err and "postgres://" not in err
    assert steps[1]["mismatch"] == 2


def test_step_and_list_limits():
    many = [{"step": f"s{i}", "elapsed_ms": i} for i in range(30)]
    facts = _failed_stage_facts(_with_metrics({"cutover_steps": many, "cutover_sequence_missing": [f"w{i}" for i in range(20)]}),
                                StageName.CUTOVER)
    assert len(facts["cutover_steps"]) == 12 and len(facts["cutover_sequence_missing"]) == 8


def test_error_text_is_capped():
    facts = _failed_stage_facts(_with_metrics({"cutover_steps": [{"step": "sink_stop", "error": "가" * 500}]}), StageName.CUTOVER)
    assert len(facts["cutover_steps"][0]["error"]) <= 200


def _capture(monkeypatch):
    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(parsed_output=AiDiagnosis(category="unknown", root_cause="원인", action_plan=["a", "b", "c"]))

    fake = ModuleType("anthropic")
    fake.Anthropic = lambda **_: SimpleNamespace(messages=SimpleNamespace(parse=parse))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ALPHA_AI_ENABLED", "1")
    return seen


def test_facts_reach_the_ai_request_and_the_prompt_names_them(monkeypatch):
    seen = _capture(monkeypatch)
    diagnose_run(_with_metrics({"phase": "streaming", "replication_lag_bytes": 123456}, stage=StageName.DB_MIGRATION,
                               message="CDC 지연"), use_memory=False)
    payload = json.loads(seen["messages"][0]["content"])
    assert payload["failed_stage_facts"]["replication_lag_bytes"] == 123456
    assert "failed_stage_facts" in seen["system"]


def test_request_has_no_facts_key_when_stage_left_nothing(monkeypatch):
    seen = _capture(monkeypatch)
    diagnose_run(_sample_run(), use_memory=False)
    assert "failed_stage_facts" not in json.loads(seen["messages"][0]["content"])
