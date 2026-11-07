"""scripts/register_secrets.py: 공개 저장소에 고정 비밀번호를 두지 않고 입력으로만 받습니다."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register_secrets.py"
SOURCE_PASSWORD = "source-password-marker"
TARGET_PASSWORD = "target-password-marker"


class Stdin:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


@pytest.fixture
def script(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("register_secrets_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.saved = {}
    monkeypatch.setattr(module, "put_secret", lambda ref, value: module.saved.update({ref: value}))
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SOURCE_DB_PASSWORD", "TARGET_DB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing-key.json"))
    monkeypatch.setattr(sys, "argv", ["register_secrets.py"])
    monkeypatch.setattr(sys, "stdin", Stdin(is_tty=False))
    return module


def test_without_a_terminal_missing_passwords_stop_before_anything_is_saved(script):
    with pytest.raises(SystemExit) as stopped:
        script.main()

    assert stopped.value.code not in (0, None)
    assert "SOURCE_DB_PASSWORD" in str(stopped.value.code)
    assert script.saved == {}


def test_passwords_come_from_the_environment(script, monkeypatch, capsys):
    monkeypatch.setenv("SOURCE_DB_PASSWORD", SOURCE_PASSWORD)
    monkeypatch.setenv("TARGET_DB_PASSWORD", TARGET_PASSWORD)

    script.main()

    assert script.saved["default/source-db-admin"] == {"user": "postgres", "password": SOURCE_PASSWORD}
    assert script.saved["default/target-admin"] == {"user": "postgres", "password": TARGET_PASSWORD}
    printed = capsys.readouterr().out
    assert SOURCE_PASSWORD not in printed
    assert TARGET_PASSWORD not in printed


def test_a_terminal_is_asked_and_arguments_win_over_the_environment(script, monkeypatch):
    monkeypatch.setattr(sys, "stdin", Stdin(is_tty=True))
    monkeypatch.setenv("SOURCE_DB_PASSWORD", "from-env")
    monkeypatch.setattr(sys, "argv", ["register_secrets.py", "--db-pass", SOURCE_PASSWORD])
    asked = []

    def answer(prompt):
        asked.append(prompt)
        return TARGET_PASSWORD if "TARGET_DB_PASSWORD" in prompt else ""

    monkeypatch.setattr(script.getpass, "getpass", answer)

    script.main()

    assert script.saved["default/source-db-admin"]["password"] == SOURCE_PASSWORD
    assert script.saved["default/target-admin"]["password"] == TARGET_PASSWORD
    assert not any("SOURCE_DB_PASSWORD" in prompt for prompt in asked)


def test_no_aws_keys_means_no_source_cloud_rather_than_a_fake_one(script, monkeypatch, capsys):
    monkeypatch.setenv("SOURCE_DB_PASSWORD", SOURCE_PASSWORD)
    monkeypatch.setenv("TARGET_DB_PASSWORD", TARGET_PASSWORD)

    script.main()

    assert "default/source-cloud" not in script.saved
    assert "source-cloud" in capsys.readouterr().out


def test_aws_keys_are_registered_when_given(script, monkeypatch):
    monkeypatch.setenv("SOURCE_DB_PASSWORD", SOURCE_PASSWORD)
    monkeypatch.setenv("TARGET_DB_PASSWORD", TARGET_PASSWORD)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "key-secret")

    script.main()

    assert script.saved["default/source-cloud"] == {"aws_access_key_id": "key-id", "aws_secret_access_key": "key-secret"}


def test_half_an_aws_key_stops_before_anything_is_saved(script, monkeypatch):
    monkeypatch.setenv("SOURCE_DB_PASSWORD", SOURCE_PASSWORD)
    monkeypatch.setenv("TARGET_DB_PASSWORD", TARGET_PASSWORD)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-id")

    with pytest.raises(SystemExit) as stopped:
        script.main()

    assert stopped.value.code not in (0, None)
    assert script.saved == {}
