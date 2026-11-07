"""ReasoningBank (#77): 지문, 학습 조건, 신뢰도, 저장 내용."""

import sqlite3

import pytest

from app.features.workflows.reasoning_bank import (
    ACTIONS,
    MAX_PLAN_ITEM,
    MAX_ROOT_CAUSE,
    MIN_SUCCESS_RATE,
    ReasoningBank,
    default_bank,
    fingerprint,
)

STAGE, PROVIDER = "provisioning", "gcp"
ERROR = "TimeoutError: connect to 10.0.1.5:5432 timed out after 30s (run mig-20261107-ab12cd34)"


@pytest.fixture
def bank(tmp_path):
    return ReasoningBank(tmp_path / "bank.sqlite3")


def open_and_resolve(bank, run_id="mig-1", error=ERROR, action="retry", from_memory=False, **over):
    fields = dict(category="network", root_cause="사설망 연결 시간 초과", action_plan=["a", "b", "c"])
    fields.update(over)
    bank.open_incident(run_id, STAGE, PROVIDER, error, from_memory=from_memory, **fields)
    return bank.resolve(run_id, STAGE, action)


# -- 지문 -------------------------------------------------------------------


def test_same_kind_of_error_has_the_same_fingerprint_across_hosts_ids_and_numbers():
    other = "TimeoutError: connect to 10.9.8.7:5432 timed out after 45s (run mig-20261108-ffee1100)"
    assert fingerprint(ERROR) == fingerprint(other)


def test_different_errors_have_different_fingerprints():
    assert fingerprint(ERROR) != fingerprint("PermissionError: 403 forbidden for service account")
    assert fingerprint("terraform 실행 파일을 찾을 수 없습니다: terraform") != fingerprint("CDC_AGENT_URL 미설정")


def test_fingerprint_hides_urls_refs_paths_and_quoted_values():
    a = "failed at https://a.example.com/x?token=1 with secret://run1/target-cloud in /srv/app/main.py 'abc'"
    b = "failed at https://b.example.org/y?token=2 with secret://run2/target-admin in /opt/x/y.py 'zzz'"
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_is_short_hex_and_handles_empty():
    assert len(fingerprint(ERROR)) == 16
    assert fingerprint("") == fingerprint("   ")


# -- 학습 -------------------------------------------------------------------


def test_nothing_is_remembered_until_a_diagnosed_incident_is_resolved(bank):
    assert bank.lookup(STAGE, PROVIDER, ERROR) is None
    assert bank.resolve("mig-1", STAGE, "retry") is False  # 진단 없이 풀린 건 기억하지 않음
    assert bank.lookup(STAGE, PROVIDER, ERROR) is None


def test_resolving_a_diagnosed_incident_creates_a_trusted_episode(bank):
    assert open_and_resolve(bank) is True
    episode = bank.lookup(STAGE, PROVIDER, "TimeoutError: connect to 10.2.2.2:5432 timed out after 5s (run mig-20261201-00000000)")
    assert episode is not None
    assert (episode.category, episode.action, episode.success_count, episode.fail_count) == ("network", "retry", 1, 0)
    assert episode.action_plan == ["a", "b", "c"]
    assert episode.trusted and episode.prior_success_rate == 1.0


def test_an_incident_resolves_only_once(bank):
    open_and_resolve(bank)
    assert bank.resolve("mig-1", STAGE, "retry") is False
    assert bank.lookup(STAGE, PROVIDER, ERROR).success_count == 1


def test_only_allowed_actions_are_stored(bank):
    bank.open_incident("mig-1", STAGE, PROVIDER, ERROR, category="network", root_cause="x", action_plan=["a"], from_memory=False)
    assert bank.resolve("mig-1", STAGE, "rm -rf /") is False
    assert bank.resolve("mig-1", STAGE, "그냥 다시 해보세요") is False
    assert bank.lookup(STAGE, PROVIDER, ERROR) is None
    assert set(ACTIONS) >= {"retry", "manual_input"}
    assert bank.resolve("mig-1", STAGE, "manual_input") is True  # 열린 장애는 그대로 남아 있었음


def test_a_second_success_adds_a_count_and_keeps_the_verified_explanation(bank):
    open_and_resolve(bank, "mig-1", root_cause="처음 설명")
    open_and_resolve(bank, "mig-2", root_cause="바뀐 설명", from_memory=True)
    episode = bank.lookup(STAGE, PROVIDER, ERROR)
    assert episode.success_count == 2
    assert episode.root_cause == "처음 설명"


def test_failure_after_following_a_memory_lowers_trust(bank):
    open_and_resolve(bank, "mig-1")
    for run_id in ("mig-2", "mig-3"):
        bank.open_incident(run_id, STAGE, PROVIDER, ERROR, category="network", root_cause="x", action_plan=["a"], from_memory=True)
        assert bank.fail(run_id, STAGE) is True
    episode = bank.lookup(STAGE, PROVIDER, ERROR)
    assert (episode.success_count, episode.fail_count) == (1, 2)
    assert episode.prior_success_rate < MIN_SUCCESS_RATE
    assert not episode.trusted


def test_failure_of_a_fresh_diagnosis_does_not_touch_memory(bank):
    open_and_resolve(bank, "mig-1")
    bank.open_incident("mig-2", STAGE, PROVIDER, ERROR, category="network", root_cause="x", action_plan=["a"], from_memory=False)
    assert bank.fail("mig-2", STAGE) is True
    assert bank.lookup(STAGE, PROVIDER, ERROR).fail_count == 0
    assert bank.fail("mig-2", STAGE) is False  # 열린 장애는 한 번만 닫힘


def test_memory_is_separated_by_stage_and_provider(bank):
    open_and_resolve(bank)
    assert bank.lookup("app_deploy", PROVIDER, ERROR) is None
    assert bank.lookup(STAGE, "azure", ERROR) is None


# -- 유사 사례 --------------------------------------------------------------


def test_similar_returns_other_resolved_cases_for_the_same_stage_and_provider(bank):
    open_and_resolve(bank, "mig-1", error="PermissionError: 403 forbidden", category="credentials")
    open_and_resolve(bank, "mig-2", error="CDC_AGENT_URL 미설정", category="network")
    found = bank.similar(STAGE, PROVIDER, ERROR)
    assert {e.category for e in found} == {"credentials", "network"}
    assert bank.similar(STAGE, "azure", ERROR) == []


def test_similar_leaves_out_the_same_error_and_unresolved_cases(bank):
    open_and_resolve(bank, "mig-1")
    bank.open_incident("mig-2", STAGE, PROVIDER, "다른 오류", category="unknown", root_cause="x", action_plan=["a"], from_memory=False)
    assert bank.similar(STAGE, PROVIDER, ERROR) == []


def test_similar_is_limited_and_ordered_by_success_count(bank):
    for i in range(5):
        open_and_resolve(bank, f"mig-{i}", error=f"error kind {chr(97 + i)}")
    for i in range(3):  # kind a 를 세 번 더 푼다
        open_and_resolve(bank, f"mig-x{i}", error="error kind a", from_memory=True)
    found = bank.similar(STAGE, PROVIDER, "something else", limit=10)
    assert len(found) == 3
    assert found[0].success_count == 4


# -- 저장 내용 --------------------------------------------------------------


def test_the_raw_error_message_is_never_stored(bank, tmp_path):
    open_and_resolve(bank)
    conn = sqlite3.connect(tmp_path / "bank.sqlite3")
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert "10.0.1.5" not in dump
    assert "mig-20261107-ab12cd34" not in dump
    assert fingerprint(ERROR) in dump


def test_stored_text_is_capped(bank):
    open_and_resolve(bank, root_cause="가" * 5000, action_plan=["나" * 5000] * 8)
    episode = bank.lookup(STAGE, PROVIDER, ERROR)
    assert len(episode.root_cause) == MAX_ROOT_CAUSE
    assert len(episode.action_plan) == 3 and all(len(item) == MAX_PLAN_ITEM for item in episode.action_plan)


def test_memory_survives_reopening_the_file(bank, tmp_path):
    open_and_resolve(bank)
    again = ReasoningBank(tmp_path / "bank.sqlite3")
    assert again.lookup(STAGE, PROVIDER, ERROR).success_count == 1


# -- 설정 -------------------------------------------------------------------


def test_default_bank_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ALPHA_REASONING_BANK", "0")
    assert default_bank() is None


def test_default_bank_uses_the_configured_path(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "memory.sqlite3"
    monkeypatch.setenv("REASONING_BANK_DB_PATH", str(target))
    assert isinstance(default_bank(), ReasoningBank)
    assert target.exists()
