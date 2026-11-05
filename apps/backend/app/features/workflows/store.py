"""run 상태 저장소. 백엔드 DB의 상태 테이블 하나에 저장해 서버 재시작 후에도 남깁니다.

표준 라이브러리 sqlite3 만 씁니다 (새 의존성 없음). 백그라운드 작업과 API 요청이
같은 run 을 동시에 바꿀 수 있으므로, 읽고-바꾸고-쓰기는 `update()` 한 곳에서 잠금으로 묶습니다.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .schemas import RunStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class RunNotFound(KeyError):
    pass


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """연결 하나를 열어 커밋(오류 시 롤백)하고 닫습니다."""
        conn = sqlite3.connect(self._path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, run: RunStatus) -> RunStatus:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, state, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.state.value,
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )
        return run

    def get(self, run_id: str) -> RunStatus:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return RunStatus.model_validate_json(row[0])

    def list(self, limit: int = 50) -> list[RunStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunStatus.model_validate_json(r[0]) for r in rows]

    def update(self, run_id: str, change: Callable[[RunStatus], RunStatus]) -> RunStatus:
        """현재 run 을 읽어 `change` 가 돌려준 새 run 으로 바꿉니다.

        읽기·쓰기를 한 트랜잭션(BEGIN IMMEDIATE)으로 묶어 다른 연결의 쓰기와 섞이지 않게 합니다.
        `change` 가 예외를 내면 아무것도 바꾸지 않습니다.
        """
        with self._lock:
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if row is None:
                    raise RunNotFound(run_id)
                new = change(RunStatus.model_validate_json(row[0]))
                conn.execute(
                    "UPDATE runs SET state = ?, data = ?, updated_at = ? WHERE run_id = ?",
                    (new.state.value, new.model_dump_json(), new.updated_at.isoformat(), run_id),
                )
                conn.execute("COMMIT")
                return new
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
