"""Terminal view of a migration's progress.

    cd apps/backend && python -m app.features.db_migration.watch <run_id>

It only reads. Closing it does not stop the migration. Needs the same CDC_AGENT_* environment
as the backend.
"""

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TextIO

import httpx

from .client import AgentError, AgentSettings, CdcAgentClient
from .settings import MissingSetting
from .guide_workflow import TERMINAL_STATES, to_job_id

PHASES = ("source_prepare", "target_prepare", "precheck", "replication_setup", "snapshot", "streaming",
          "cutover_ready")
BAR_WIDTH = 24
CLEAR_SCREEN = "\033[2J\033[H"


def _steps(job: dict) -> str:
    current = PHASES.index(job["phase"]) if job["phase"] in PHASES else len(PHASES)
    marks = []
    for i, phase in enumerate(PHASES):
        if i < current:
            mark = "✓"
        elif i > current:
            mark = "○"
        else:
            mark = "✗" if job["state"] == "failed" else "●"
        marks.append(f"{phase} {mark}")
    return "  ".join(marks)


def _bar(percent: int) -> str:
    filled = BAR_WIDTH * percent // 100
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _table_state(row: dict) -> str:
    if row["match"]:
        return "일치"
    return "검증 전" if row["percent"] == 100 else "복사 중"


def _copy(metrics: dict) -> list[str]:
    tables = metrics.get("tables")
    if not tables:
        return ["복사 시작 전"]
    done = sum(min(row["target_rows"], row["source_rows"]) for row in tables.values())
    total = sum(row["source_rows"] for row in tables.values())
    width = max(len(name) for name in tables)
    lines = [f"전체 {_bar(metrics['copy_percent'])} {metrics['copy_percent']:>3}%  {done:,} / {total:,}행"]
    for name, row in tables.items():
        lines.append(f"  {name:<{width}}  {_bar(row['percent'])} {row['percent']:>3}%  "
                     f"{row['target_rows']:,} / {row['source_rows']:,}행  {_table_state(row)}")
    return lines


def _size(value: int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _status(metrics: dict, now: datetime) -> list[str]:
    lines = []
    if "mismatch" in metrics:
        age = ""
        if metrics.get("measured_at"):
            age = f"   측정 {max(0, int((now - datetime.fromisoformat(metrics['measured_at'])).total_seconds()))}초 전"
        lines.append(f"복제 지연 {_size(metrics.get('replication_lag_bytes'))}   불일치 테이블 {metrics['mismatch']}개{age}")
    if metrics.get("measure_error"):
        lines.append(f"측정 오류: {metrics['measure_error']}")
    return lines


def render(job: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    metrics = job.get("metrics") or {}
    lines = [f"{job['job_id']}   {job['state']}   {job['phase']}", _steps(job), "", *_copy(metrics), *_status(metrics, now)]
    if job.get("message"):
        lines += ["", job["message"]]
    return "\n".join(lines)


def watch(run_id: str, agent: CdcAgentClient, *, interval_s: float = 10, once: bool = False,
          out: TextIO = sys.stdout, sleep: Callable[[float], None] = time.sleep) -> int:
    """Returns the exit code: 0 unless the job failed or could not be read in once mode."""
    job_id = to_job_id(run_id)
    live = not once and out.isatty()
    while True:
        job = None
        try:
            job = agent.get_job(job_id)
            text = render(job)
        except AgentError as exc:
            if exc.status_code == 404:
                text = f"{job_id}: 작업 등록 전입니다. VPN을 준비 중일 수 있습니다."
            elif exc.status_code < 500:
                print(f"조회 실패: {exc}", file=out)
                return 1
            else:
                text = f"에이전트 응답 오류, 다시 시도합니다: {exc}"
        except httpx.HTTPError as exc:
            text = f"에이전트 연결 오류, 다시 시도합니다: {exc}"
        print((CLEAR_SCREEN if live else "") + text, file=out, flush=True)
        if job is not None and job["state"] in TERMINAL_STATES:
            return 1 if job["state"] == "failed" else 0
        if once:
            return 0 if job is not None else 1
        sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watch", description="데이터 이전 진행상황을 터미널에 표시합니다.")
    parser.add_argument("run_id")
    parser.add_argument("--interval", type=float, default=10, help="조회 간격(초), 기본 10")
    parser.add_argument("--once", action="store_true", help="한 번만 출력하고 끝냅니다")
    args = parser.parse_args(argv)
    try:
        agent = CdcAgentClient(AgentSettings.from_env())
    except MissingSetting:
        print("CDC_AGENT_URL이 설정되지 않았습니다.", file=sys.stderr)
        return 2
    try:
        return watch(args.run_id, agent, interval_s=args.interval, once=args.once)
    except KeyboardInterrupt:
        print("\n화면만 닫습니다. 이전 작업은 계속됩니다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
