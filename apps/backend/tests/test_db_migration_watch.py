import io
from datetime import datetime, timezone

import httpx

from app.features.db_migration.client import AgentSettings, CdcAgentClient
from app.features.db_migration.watch import main, render, watch

NOW = datetime(2026, 11, 5, 3, 0, 10, tzinfo=timezone.utc)
COPYING = {
    "job_id": "mig_001", "state": "running", "phase": "streaming", "progress": 50, "message": "CDC 동기화 중",
    "metrics": {
        "copy_percent": 40, "mismatch": 2, "replication_lag_bytes": 2048,
        "measured_at": "2026-11-05T03:00:07.000+00:00",
        "tables": {
            "public.orders": {"source_rows": 1000, "target_rows": 400, "percent": 40, "match": False},
            "public.users": {"source_rows": 0, "target_rows": 0, "percent": 100, "match": False},
        },
    },
}


def agent(*responses) -> CdcAgentClient:
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    return CdcAgentClient(AgentSettings(url="https://cdc.example"), transport=httpx.MockTransport(handle))


def test_render_shows_each_table_and_the_total_while_copying():
    text = render(COPYING, NOW)

    assert "전체" in text and "40%" in text and "400 / 1,000행" in text
    assert "public.orders" in text and "복사 중" in text
    assert "public.users" in text and "검증 전" in text
    assert "복제 지연 2KB" in text and "불일치 테이블 2개" in text and "측정 3초 전" in text
    assert "snapshot ✓" in text and "streaming ●" in text and "cutover_ready ○" in text


def test_render_before_copy_starts_has_no_metrics():
    job = {"job_id": "j", "state": "running", "phase": "precheck", "message": "입력 검사", "metrics": {}}
    text = render(job, NOW)

    assert "복사 시작 전" in text and "precheck ●" in text and "입력 검사" in text


def test_render_marks_the_failed_step_and_a_finished_table():
    job = {**COPYING, "state": "failed", "phase": "snapshot", "message": "snapshot: boom", "metrics": {
        "copy_percent": 100, "tables": {"public.a": {"source_rows": 5, "target_rows": 5, "percent": 100, "match": True}},
    }}
    text = render(job, NOW)

    assert "snapshot ✗" in text and "일치" in text and "snapshot: boom" in text


def test_render_after_cutover_marks_every_listed_step_done():
    job = {"job_id": "j", "state": "running", "phase": "stabilizing", "metrics": {}}
    assert "cutover_ready ✓" in render(job, NOW)


def test_watch_waits_through_a_missing_job_and_errors_until_the_job_ends():
    out, naps = io.StringIO(), []
    client = agent((404, {"detail": "not found"}), (502, {"detail": "bad gateway"}),
                   httpx.ConnectError("down"), (200, COPYING),
                   (200, {**COPYING, "state": "aborted", "message": "중단됨"}))

    assert watch("mig-001", client, interval_s=7, out=out, sleep=naps.append) == 0

    text = out.getvalue()
    assert "작업 등록 전" in text and "응답 오류" in text and "연결 오류" in text and "중단됨" in text
    assert naps == [7, 7, 7, 7]


def test_watch_exit_codes():
    failed = {**COPYING, "state": "failed"}
    assert watch("mig-001", agent((200, failed)), out=io.StringIO(), sleep=lambda s: None) == 1
    assert watch("mig-001", agent((401, {"detail": "no"})), out=io.StringIO(), sleep=lambda s: None) == 1
    assert watch("mig-001", agent((200, COPYING)), once=True, out=io.StringIO()) == 0
    assert watch("mig-001", agent((404, {"detail": "x"})), once=True, out=io.StringIO()) == 1


def test_screen_is_cleared_only_on_a_live_terminal():
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    done = {**COPYING, "state": "aborted"}
    live, piped = Terminal(), io.StringIO()
    watch("mig-001", agent((200, done)), out=live)
    watch("mig-001", agent((200, done)), out=piped)

    assert live.getvalue().startswith("\033[2J") and "\033" not in piped.getvalue()


def test_main_says_what_is_missing_without_the_agent_url(monkeypatch, capsys):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)

    assert main(["mig-001", "--once"]) == 2
    assert "CDC_AGENT_URL" in capsys.readouterr().err
