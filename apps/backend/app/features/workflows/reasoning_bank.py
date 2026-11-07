"""ReasoningBank: 자가치유 진단의 장기 기억 (#77).

한 번 진단해서 실제로 풀린 장애를 기억해 두었다가, 같은 장애가 다시 나면 같은 조치를 먼저 제안합니다.

- 기억하는 단위는 (단계, 목적지 클라우드, 에러 지문) 입니다. 에러 지문은 호스트·ID·경로·숫자를 지운 문장의 해시라서
  원문 에러 메시지는 저장하지 않습니다.
- 학습은 진단까지 거친 장애만 합니다. 진단 때 `open_incident` 로 열어 두고, 그 단계가 실제로 SUCCEEDED 로 회복되면
  `resolve` 로 성공을 올립니다. 운영자가 조치를 눌렀다는 것만으로는 올리지 않습니다.
- 기억한 조치를 따랐는데 같은 단계가 다시 실패하면 `fail` 로 실패를 올려 신뢰도를 낮춥니다.
- 저장하는 조치(`action`)는 허용 목록(ACTIONS) 안의 값뿐입니다. AI 가 만든 자유 문장은 조치로 저장하지 않고,
  원인·조치 문장은 호출하는 쪽이 살균한 것을 길이만 잘라 저장합니다.
- 기억이 틀려도 진단이 막히지 않도록, 기억 저장소 오류는 호출부가 삼키고 규칙·AI 진단으로 넘어갑니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

# 실제로 관찰할 수 있는 조치만 기억합니다 (resume → retry, 수동 입력 → manual_input).
ACTIONS = ("retry", "manual_input", "cleanup_cdc", "reauth")

# 기억을 그대로 따르는 최소 조건. 한 번이라도 풀렸고, 성공률이 이 값 이상이어야 합니다.
MIN_SUCCESSES = 1
MIN_SUCCESS_RATE = 0.6

MAX_FINGERPRINT_TEXT = 300
MAX_ROOT_CAUSE = 400
MAX_PLAN_ITEM = 300
MAX_PLAN_ITEMS = 3
MAX_EXEMPLARS = 3

# 지문에서 지우는 값들. 순서가 중요합니다 (URL → 경로 → 숫자 순으로 큰 것부터).
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"secret://\S+"), "<ref>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\bmig-[0-9a-z-]+\b"), "<run>"),
    (re.compile(r"[a-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"(?<![\w<])/[\w.\-]+(?:/[\w.\-]+)+"), "<path>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<q>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\d+"), "#"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reasoning_episodes (
    stage         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    category      TEXT NOT NULL,
    root_cause    TEXT NOT NULL,
    action_plan   TEXT NOT NULL,
    action        TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (stage, provider, fingerprint)
);
CREATE TABLE IF NOT EXISTS reasoning_incidents (
    run_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    provider    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    category    TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    action_plan TEXT NOT NULL,
    from_memory INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, stage)
)
"""


def fingerprint(error_msg: str) -> str:
    """에러 문장에서 호스트·ID·경로·숫자를 지운 뒤 해시합니다. 같은 종류의 장애면 같은 값이 됩니다."""
    text = (error_msg or "").lower()
    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()[:MAX_FINGERPRINT_TEXT]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Episode(BaseModel):
    stage: str
    provider: str
    fingerprint: str
    category: str
    root_cause: str
    action_plan: list[str]
    action: str
    success_count: int
    fail_count: int

    @property
    def prior_success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total else 0.0

    @property
    def trusted(self) -> bool:
        """LLM 을 건너뛰고 그대로 제안해도 되는 기억인지."""
        return self.success_count >= MIN_SUCCESSES and self.prior_success_rate >= MIN_SUCCESS_RATE


def _cap(text: str, limit: int) -> str:
    return text.strip()[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReasoningBank:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- 조회 ---------------------------------------------------------------

    def lookup(self, stage: str, provider: str, error_msg: str) -> Episode | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT stage, provider, fingerprint, category, root_cause, action_plan, action, success_count, fail_count "
                "FROM reasoning_episodes WHERE stage = ? AND provider = ? AND fingerprint = ?",
                (stage, provider, fingerprint(error_msg)),
            ).fetchone()
        return _episode(row) if row else None

    def similar(self, stage: str, provider: str, error_msg: str, limit: int = MAX_EXEMPLARS) -> list[Episode]:
        """같은 단계·같은 클라우드에서 풀렸던 다른 사례. 새 장애를 AI 에게 물을 때 참고 예시로만 씁니다."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT stage, provider, fingerprint, category, root_cause, action_plan, action, success_count, fail_count "
                "FROM reasoning_episodes WHERE stage = ? AND provider = ? AND fingerprint != ? AND success_count >= ? "
                "ORDER BY success_count DESC, updated_at DESC LIMIT ?",
                (stage, provider, fingerprint(error_msg), MIN_SUCCESSES, min(limit, MAX_EXEMPLARS)),
            ).fetchall()
        return [_episode(r) for r in rows]

    # -- 학습 ---------------------------------------------------------------

    def open_incident(
        self,
        run_id: str,
        stage: str,
        provider: str,
        error_msg: str,
        *,
        category: str,
        root_cause: str,
        action_plan: list[str],
        from_memory: bool,
    ) -> None:
        """진단을 낸 장애를 열어 둡니다. 이 단계가 나중에 풀리면 resolve() 가 기억으로 올립니다."""
        plan = [_cap(item, MAX_PLAN_ITEM) for item in action_plan[:MAX_PLAN_ITEMS]]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reasoning_incidents "
                "(run_id, stage, provider, fingerprint, category, root_cause, action_plan, from_memory, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    stage,
                    provider,
                    fingerprint(error_msg),
                    category,
                    _cap(root_cause, MAX_ROOT_CAUSE),
                    json.dumps(plan, ensure_ascii=False),
                    1 if from_memory else 0,
                    _now(),
                ),
            )

    def resolve(self, run_id: str, stage: str, action: str) -> bool:
        """열려 있던 장애의 단계가 실제로 회복됐을 때만 부릅니다. 기억할 수 있는 조치(ACTIONS)만 받습니다."""
        if action not in ACTIONS:
            return False
        with self._lock, self._connect() as conn:
            incident = conn.execute(
                "SELECT provider, fingerprint, category, root_cause, action_plan FROM reasoning_incidents "
                "WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            if incident is None:
                return False
            provider, fp, category, root_cause, plan = incident
            now = _now()
            exists = conn.execute(
                "SELECT success_count FROM reasoning_episodes WHERE stage = ? AND provider = ? AND fingerprint = ?",
                (stage, provider, fp),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO reasoning_episodes "
                    "(stage, provider, fingerprint, category, root_cause, action_plan, action, success_count, fail_count, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                    (stage, provider, fp, category, root_cause, plan, action, now, now),
                )
            else:
                # 이미 검증된 설명은 바꾸지 않고 횟수만 올립니다 (같은 장애의 설명이 흔들리지 않게).
                conn.execute(
                    "UPDATE reasoning_episodes SET success_count = success_count + 1, action = ?, updated_at = ? "
                    "WHERE stage = ? AND provider = ? AND fingerprint = ?",
                    (action, now, stage, provider, fp),
                )
            conn.execute("DELETE FROM reasoning_incidents WHERE run_id = ? AND stage = ?", (run_id, stage))
        return True

    def fail(self, run_id: str, stage: str) -> bool:
        """열려 있던 장애의 단계가 다시 실패했을 때 부릅니다. 기억을 따른 경우에만 그 기억의 실패를 올립니다."""
        with self._lock, self._connect() as conn:
            incident = conn.execute(
                "SELECT provider, fingerprint, from_memory FROM reasoning_incidents WHERE run_id = ? AND stage = ?",
                (run_id, stage),
            ).fetchone()
            if incident is None:
                return False
            provider, fp, from_memory = incident
            if from_memory:
                conn.execute(
                    "UPDATE reasoning_episodes SET fail_count = fail_count + 1, updated_at = ? "
                    "WHERE stage = ? AND provider = ? AND fingerprint = ?",
                    (_now(), stage, provider, fp),
                )
            conn.execute("DELETE FROM reasoning_incidents WHERE run_id = ? AND stage = ?", (run_id, stage))
        return True


def _episode(row: tuple) -> Episode:
    stage, provider, fp, category, root_cause, plan, action, success, failed = row
    return Episode(
        stage=stage,
        provider=provider,
        fingerprint=fp,
        category=category,
        root_cause=root_cause,
        action_plan=json.loads(plan),
        action=action,
        success_count=success,
        fail_count=failed,
    )


def default_bank() -> ReasoningBank | None:
    """설정에 맞는 기억 저장소. `ALPHA_REASONING_BANK=0` 이면 끕니다."""
    if os.environ.get("ALPHA_REASONING_BANK", "1") == "0":
        return None
    path = os.environ.get("REASONING_BANK_DB_PATH")
    if not path:
        from app.core.config import get_settings

        path = get_settings().workflows_db_path
    return ReasoningBank(path)
