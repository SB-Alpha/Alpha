"""원본 RDS 접속용 cursor 제공 (읽기 전용).

# ponytail: psql 폴백은 psycopg 합의 전 임시. psycopg 합의되면 폴백 제거

- psycopg(v3)가 설치돼 있으면 사용(lazy import), 없으면 psql 서브프로세스 폴백.
- 비밀번호는 서브프로세스 env `PGPASSWORD`로만 전달한다. argv/예외/로그에 남기지 않는다.
- 읽기 전용: `default_transaction_read_only=on`을 연결 옵션(PGOPTIONS / psycopg options)으로 건다.
- `collect_catalog`는 실패를 삼키지 않고 예외(RuntimeError 등)를 그대로 올린다.
  호출자가 잡아서 gap(`{"item": "tables", "reason": ...}`)으로 처리한다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from typing import Any, Iterator

from app.features.assessment import pg_tables

PSQL_TIMEOUT_SEC = 15  # psql 1회 호출 상한(접속 불가 시 최악 시간 단축)
NULL = "__NULL__"
SEP = "\x1f"
# 읽기 전용 + 쿼리 하나당 15초 제한 (서버가 응답을 멈추면 connect_timeout 만으로는 끝나지 않음)
STATEMENT_TIMEOUT_MS = 15000
RO_OPTION = f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
# 네트워크가 끊겨 응답이 아예 오지 않을 때 연결을 끊기 위한 libpq keepalive
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3}
_BREW_PSQL = "/opt/homebrew/opt/libpq/bin/psql"
NEED_DRIVER = "psycopg 또는 psql 필요 — psycopg는 팀 합의 대기"


# host/user/database는 psql -h/-U/-d 인자(conninfo 해석 가능)로 가므로 보수적으로 검증한다
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_.$@:][A-Za-z0-9_.$@:-]*$")
# 자식 프로세스에 넘기는 환경변수(다른 SOURCE_* 시크릿 차단)
_ENV_KEEP = ("PATH", "HOME", "LANG", "PGCONNECT_TIMEOUT")


def _check_arg(name: str, v: str) -> None:
    if not isinstance(v, str) or not _SAFE_ARG.fullmatch(v):
        raise ValueError(f"{name} 형식이 올바르지 않음")


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith("LC_")}


def _find_psql() -> str | None:
    return shutil.which("psql") or (_BREW_PSQL if os.access(_BREW_PSQL, os.X_OK) else None)


class PsqlCursor:
    """psql 서브프로세스로 SQL을 실행하는 최소 cursor(execute/fetchall/fetchone)."""

    def __init__(self, psql: str, host: str, port: int, user: str, password: str,
                 database: str, sslmode: str, timeout: int = PSQL_TIMEOUT_SEC):
        for n, v in (("host", host), ("user", user), ("database", database)):
            _check_arg(n, v)
        self._psql, self._timeout = psql, timeout
        self._args = ["-h", host, "-p", str(port), "-U", user, "-d", database]
        # 비밀번호/옵션은 argv가 아니라 env로만 전달
        self._env = {**_child_env(), "PGPASSWORD": password, "PGSSLMODE": sslmode,
                     "PGOPTIONS": RO_OPTION}
        self.rows: list[tuple] = []

    def execute(self, sql: str) -> None:
        try:
            r = subprocess.run(
                [self._psql, "-X", *self._args, "-At", "-F", SEP, "-P", f"null={NULL}",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True, env=self._env, timeout=self._timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"psql timeout ({self._timeout}s)") from None
        if r.returncode:
            lines = (r.stderr or "").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "psql failed")
        self.rows = [tuple(None if v == NULL else v for v in line.split(SEP))
                     for line in r.stdout.splitlines() if line]

    def fetchall(self) -> list[tuple]:
        return self.rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None


@contextmanager
def open_cursor(host: str, port: int, user: str, password: str,
                database: str = "postgres", sslmode: str = "require") -> Iterator[Any]:
    """읽기 전용 cursor 컨텍스트 매니저. psycopg 우선, 없으면 psql 폴백."""
    for n, v in (("host", host), ("user", user), ("database", database)):
        _check_arg(n, v)
    try:
        import psycopg  # type: ignore[import-not-found]  # 선택적 lazy import
    except ImportError:
        psycopg = None
    if psycopg is not None:
        with psycopg.connect(host=host, port=port, user=user, password=password,
                             dbname=database, sslmode=sslmode, options=RO_OPTION,
                             autocommit=True, connect_timeout=15, **KEEPALIVE) as conn:
            with conn.cursor() as cur:
                yield cur
        return
    psql = _find_psql()
    if psql is None:
        raise RuntimeError(NEED_DRIVER)
    yield PsqlCursor(psql, host, port, user, password, database, sslmode)


def collect_catalog(host: str, port: int, user: str, password: str,
                    database: str = "postgres", cloud: str = "aws") -> dict:
    """접속해 카탈로그를 수집한다. 연결/실행 실패는 예외로 올라간다(호출자가 gap 처리)."""
    with open_cursor(host, port, user, password, database) as cur:
        return pg_tables.collect_pg_catalog(cur, cloud)
