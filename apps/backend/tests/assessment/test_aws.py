"""aws.session_for: 이름표 → 세션. boto3 · core/secrets 는 가짜 모듈로 바꿔 끼웁니다."""

import sys
import types

from app.features.assessment import aws


class FakeBotoSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_modules(monkeypatch, stored):
    boto3 = types.ModuleType("boto3")
    boto3.Session = FakeBotoSession
    secrets = types.ModuleType("app.core.secrets")
    secrets.resolve = lambda ref: stored[ref]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "app.core.secrets", secrets)


def test_secret_ref_resolves_dict_from_secret_store(monkeypatch):
    # PR #29 resolve() 는 dict 를 돌려줌
    _fake_modules(monkeypatch, {"secret://run-1/source-cloud": {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "x"}})
    sess, why = aws.session_for("secret://run-1/source-cloud", "ap-northeast-2")
    assert why is None
    assert sess.raw.kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert sess.raw.kwargs["aws_session_token"] is None and sess.raw.kwargs["region_name"] == "ap-northeast-2"


def test_missing_secret_becomes_gap_without_value(monkeypatch):
    _fake_modules(monkeypatch, {})
    sess, why = aws.session_for("secret://run-1/source-cloud", "ap-northeast-2")
    assert sess is None and "KeyError" in why


def test_unknown_ref_format_is_gap(monkeypatch):
    _fake_modules(monkeypatch, {})
    sess, why = aws.session_for("arn:aws:iam::123456789012:role/x", "ap-northeast-2")
    assert sess is None and "지원하지 않는" in why


def test_secret_ref_accepts_db_migration_field_names(monkeypatch):
    # #31 source-cloud 저장 형식 (aws_ 접두사)
    _fake_modules(monkeypatch, {"secret://run-1/source-cloud": {
        "aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "x", "aws_session_token": "t"}})
    sess, why = aws.session_for("secret://run-1/source-cloud", "ap-northeast-2")
    assert why is None
    assert sess.raw.kwargs["aws_access_key_id"] == "AKIAEXAMPLE" and sess.raw.kwargs["aws_session_token"] == "t"


def test_profile_ref_refused_outside_cli(monkeypatch):
    _fake_modules(monkeypatch, {})
    monkeypatch.delenv(aws.ALLOW_PROFILE_ENV, raising=False)
    sess, why = aws.session_for("profile:default", "ap-northeast-2")
    assert sess is None and "로컬 CLI" in why


def test_profile_ref_allowed_with_flag(monkeypatch):
    _fake_modules(monkeypatch, {})
    monkeypatch.setenv(aws.ALLOW_PROFILE_ENV, "1")
    sess, why = aws.session_for("profile:dev", "ap-northeast-2")
    assert why is None and sess.raw.kwargs["profile_name"] == "dev"


def test_session_is_wrapped_with_timeouts(monkeypatch):
    _fake_modules(monkeypatch, {"secret://run-1/source-cloud": {"aws_access_key_id": "AKIAEXAMPLE", "aws_secret_access_key": "x"}})
    sess, _ = aws.session_for("secret://run-1/source-cloud", "ap-northeast-2")
    assert isinstance(sess, aws.CachedSession)
    cfg = sess._config
    assert cfg.connect_timeout == aws.CONNECT_TIMEOUT_S and cfg.read_timeout == aws.READ_TIMEOUT_S
    assert cfg.retries == {"total_max_attempts": aws.TOTAL_ATTEMPTS, "mode": "standard"}


def test_cached_session_builds_each_client_once_across_threads():
    import threading
    import time

    class Raw:
        made = 0

        def client(self, name, **kw):
            Raw.made += 1
            time.sleep(0.01)  # 잠금이 없으면 여러 스레드가 동시에 만듦
            return (name, kw.get("region_name"), kw.get("config"))

    sess = aws.CachedSession(Raw(), config="cfg")
    threads = [threading.Thread(target=sess.client, args=("ec2", "ap-northeast-2")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Raw.made == 1
    assert sess.client("ec2", "ap-northeast-2") == ("ec2", "ap-northeast-2", "cfg")
    sess.client("ecs")
    assert Raw.made == 2
