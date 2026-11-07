"""pg_connect 테스트. subprocess.run을 가짜로 대체하며 실제 DB에는 접속하지 않는다."""

import subprocess
from types import SimpleNamespace

import pytest

from app.features.assessment import pg_connect as m

PW = "s3cret-pw"


def fake_run(captured, stdout="", returncode=0, stderr=""):
    def run(argv, **kw):
        captured.update(argv=argv, **kw)
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
    return run


def cursor():
    return m.PsqlCursor("/x/psql", "h", 5432, "u", PW, "postgres", "require")


def test_password_only_in_env_and_readonly(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", fake_run(cap, "a\x1f__NULL__\x1fc\n"))
    c = cursor()
    c.execute("SELECT 1")
    assert not any(PW in a for a in cap["argv"])
    assert cap["env"]["PGPASSWORD"] == PW and cap["env"]["PGSSLMODE"] == "require"
    assert "default_transaction_read_only=on" in cap["env"]["PGOPTIONS"]
    assert cap["timeout"] and c.fetchall() == [("a", None, "c")]
    assert c.fetchone() == ("a", None, "c")


def test_error_message_has_no_password(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", fake_run(cap, returncode=2, stderr=f"l1\npsql: FATAL: auth failed\n"))
    with pytest.raises(RuntimeError) as e:
        cursor().execute("SELECT 1")
    assert str(e.value) == "psql: FATAL: auth failed" and PW not in str(e.value)


def test_timeout_message(monkeypatch):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as e:
        cursor().execute("SELECT 1")
    assert PW not in str(e.value)


def test_no_driver(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)  # import 실패 유도
    monkeypatch.setattr(m, "_find_psql", lambda: None)
    with pytest.raises(RuntimeError, match="psycopg 또는 psql 필요"):
        with m.open_cursor("h", 1, "u", PW):
            pass


def test_psql_fallback_used(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
    monkeypatch.setattr(m, "_find_psql", lambda: "/x/psql")
    with m.open_cursor("h", 1, "u", PW) as cur:
        assert isinstance(cur, m.PsqlCursor)


def test_child_env_minimal(monkeypatch):
    monkeypatch.setenv("SOURCE_CLOUD_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("LC_ALL", "C")
    cap = {}
    monkeypatch.setattr(subprocess, "run", fake_run(cap))
    cursor().execute("SELECT 1")
    assert "SOURCE_CLOUD_SECRET_ACCESS_KEY" not in cap["env"] and "leak-me" not in cap["env"].values()
    assert cap["env"]["LC_ALL"] == "C" and cap["env"]["PGPASSWORD"] == PW


@pytest.mark.parametrize("bad", ["-evil", "a b", "dbname=x host=y", "a=b", "", "a\nb"])
def test_args_validated(bad):
    for kw in ({"host": bad}, {"user": bad}, {"database": bad}):
        a = {"host": "h", "user": "u", "database": "postgres", **kw}
        with pytest.raises(ValueError):
            m.PsqlCursor("/x/psql", a["host"], 5432, a["user"], PW, a["database"], "require")
    with pytest.raises(ValueError):
        with m.open_cursor("h", 1, "u", PW, bad):
            pass


def test_queries_have_a_statement_timeout():
    # 접속 뒤 서버가 응답을 멈춰도 터널이 무한히 열려 있지 않게 (psycopg options · psql PGOPTIONS 공통)
    assert "statement_timeout=" in m.RO_OPTION and "default_transaction_read_only=on" in m.RO_OPTION
