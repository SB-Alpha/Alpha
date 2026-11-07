"""터미널에서 DB 이전을 돌리고, 절차와 UI로 가는 값을 시간순으로 찍는다.

    cd apps/backend && set -a && source .env && set +a
    python -m app.features.db_migration.cli_viewer run input.json | tee run.log
    python -m app.features.db_migration.cli_viewer status <run_id>
    python -m app.features.db_migration.cli_viewer cutover <run_id> --by <승인자>
    python -m app.features.db_migration.cli_viewer finish <run_id>
    python -m app.features.db_migration.cli_viewer stop <run_id>

줄의 머리표:
    [절차]       이 모듈의 진행 로그 (progress_log.py)
    [UI:status]  GET /runs/{run_id} 응답. 값이 바뀔 때만 찍는다.
    [UI:report]  workflows로 보내는 보고. 값이 바뀔 때만 찍는다.
    [UI:cutover] [UI:finish] [UI:stop]  같은 이름의 API 응답

run은 cutover_ready나 끝 상태에서 돌아온다. 복제는 에이전트에서 계속되므로 그 뒤에 닫아도 된다.
VPN 구축 중에 닫으면 Terraform이 중단될 수 있다. 입력에 run_id를 넣어 다시 실행하거나 stop으로 정리한다.
입력 파일에는 DB 비밀번호가 있으므로 저장소 밖에 두고, 끝나면 지운다.
"""

import argparse
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import httpx
from pydantic import BaseModel

from . import db_workflow, redact
from .client import AgentError, AgentSettings, CdcAgentClient
from .progress_log import logger as progress_logger
from .run_input import MissingCredentials
from .schemas import RunStatus
from .settings import MissingSetting

ENDED_STATES = {"succeeded", "failed", "aborted", "stopped"}
# 매번 바뀌는 측정 시각만으로는 다시 찍지 않는다.
_CHURN = {"measured_at"}
_EXPECTED_ERRORS = (
    db_workflow.InvalidInput, db_workflow.NotAllowed, db_workflow.UnknownRun, MissingCredentials, MissingSetting,
    AgentError, httpx.HTTPError, OSError, ValueError,
)
_EVENT_ENVELOPE = {"ts", "event", "run_id", "job_id"}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _without_churn(metrics: Any) -> Any:
    if not isinstance(metrics, dict):
        return metrics
    return {key: value for key, value in metrics.items() if key not in _CHURN}


class Viewer:
    """찍는 곳은 여기 하나다. 모든 줄이 비밀값 제거를 거친다. db_workflow의 Reporter이기도 하다."""

    def __init__(self, out: TextIO, run_id: str | None = None):
        self.out = out
        self.run_id = run_id
        self._lock = threading.Lock()
        self._last_report: Any = None
        self._last_status: Any = None

    def _masked(self, text: str) -> str:
        return db_workflow._clean(self.run_id, text) if self.run_id else redact.clean(text)

    def emit(self, tag: str, text: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} [{tag}] {self._masked(text)}"
        with self._lock:
            print(line, file=self.out, flush=True)

    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None,
               message: str | None = None) -> None:
        key = (state, progress, _without_churn(metrics), message)
        with self._lock:
            if key == self._last_report:
                return
            self._last_report = key
        self.emit("UI:report", _dump({"state": state, "progress": progress, "message": message, "metrics": metrics}))

    def status(self, status: RunStatus) -> None:
        data = status.model_dump()
        key = {**data, "metrics": _without_churn(data["metrics"])}
        if key != self._last_status:
            self._last_status = key
            self.emit("UI:status", _dump(data))

    def result(self, tag: str, result: BaseModel) -> None:
        self.emit(tag, _dump(result.model_dump()))

    def event(self, line: dict[str, Any]) -> None:
        self.run_id = self.run_id or line.get("run_id")
        fields = {key: value for key, value in line.items() if key not in _EVENT_ENVELOPE}
        self.emit("절차", f"{line['event']} {_dump(fields)}" if fields else line["event"])


class _ViewerHandler(logging.Handler):
    def __init__(self, viewer: Viewer):
        super().__init__(logging.INFO)
        self.viewer = viewer

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        try:
            line = json.loads(text)
        except ValueError:
            line = None
        if isinstance(line, dict) and "event" in line:
            self.viewer.event(line)
        else:
            self.viewer.emit("로그", f"{record.levelname} {text}")


@contextmanager
def attached(viewer: Viewer) -> Iterator[None]:
    """이 모듈의 로그를 화면으로만 보낸다. 같은 줄이 두 번 찍히지 않게 전달을 끈다."""
    module_logger = progress_logger
    level, propagate = module_logger.level, module_logger.propagate
    handler = _ViewerHandler(viewer)
    module_logger.setLevel(logging.INFO)
    module_logger.propagate = False
    module_logger.addHandler(handler)
    try:
        yield
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(level)
        module_logger.propagate = propagate


def _in_background(call: Callable[[], Any]) -> Future:
    future: Future = Future()

    def work() -> None:
        try:
            future.set_result(call())
        except BaseException as exc:  # noqa: BLE001 - 호출한 쪽의 result()에서 다시 던진다
            future.set_exception(exc)

    threading.Thread(target=work, daemon=True, name="cli-viewer-call").start()
    return future


def _watching(viewer: Viewer, run_id: str, agent: CdcAgentClient, interval_s: float, call: Callable[[], BaseModel],
              sleep: Callable[[float], None]) -> BaseModel:
    """오래 걸리는 호출이 도는 동안 상태 변화를 찍는다."""
    future = _in_background(call)
    while not future.done():
        try:
            viewer.status(db_workflow.status(run_id, agent=agent))
        except db_workflow.UnknownRun:
            pass  # 정리가 끝나 기록이 지워졌을 수 있다. 결과는 call이 답한다.
        sleep(interval_s)
    return future.result()


def cmd_run(args: argparse.Namespace, viewer: Viewer, agent: CdcAgentClient, sleep: Callable[[float], None]) -> int:
    body = json.loads(Path(args.input).read_text(encoding="utf-8"))
    run_id = db_workflow.run(body, agent=agent, ctx=viewer)
    viewer.run_id = run_id
    viewer.emit("안내", f"run_id={run_id}")
    while True:
        status = db_workflow.status(run_id, agent=agent)
        viewer.status(status)
        if status.ready_for_cutover:
            viewer.emit("안내", f"전환 대기 상태입니다. 다음: cutover {run_id} --by <승인자> 또는 stop {run_id}")
            return 0
        if status.state in ENDED_STATES:
            if status.cleanup_required:
                viewer.emit("안내", f"남은 자원 {status.remaining}. 정리: stop {run_id}")
            return 1 if status.state == "failed" else 0
        sleep(args.interval)


def cmd_status(args: argparse.Namespace, viewer: Viewer, agent: CdcAgentClient, sleep: Callable[[float], None]) -> int:
    viewer.status(db_workflow.status(args.run_id, agent=agent))
    return 0


def cmd_cutover(args: argparse.Namespace, viewer: Viewer, agent: CdcAgentClient, sleep: Callable[[float], None]) -> int:
    result = _watching(viewer, args.run_id, agent, args.interval,
                       lambda: db_workflow.cutover(args.run_id, args.by, agent=agent), sleep)
    viewer.result("UI:cutover", result)
    return 0 if result.outcome == "switched" else 1


def cmd_finish(args: argparse.Namespace, viewer: Viewer, agent: CdcAgentClient, sleep: Callable[[float], None]) -> int:
    result = _watching(viewer, args.run_id, agent, args.interval,
                       lambda: db_workflow.finish(args.run_id, agent=agent), sleep)
    viewer.result("UI:finish", result)
    return 0 if result.state == "finished" else 1


def cmd_stop(args: argparse.Namespace, viewer: Viewer, agent: CdcAgentClient, sleep: Callable[[float], None]) -> int:
    result = _watching(viewer, args.run_id, agent, args.interval,
                       lambda: db_workflow.stop(args.run_id, agent=agent), sleep)
    viewer.result("UI:stop", result)
    return 0 if result.state == "finished" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli_viewer", description="DB 이전을 실행하고 절차와 UI 값을 로그로 찍습니다.")
    parser.add_argument("--interval", type=float, default=5, help="상태 조회 간격(초), 기본 5")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="입력 파일로 시작해 cutover_ready까지 봅니다")
    run.add_argument("input", help="POST /runs 본문과 같은 JSON 파일")
    run.set_defaults(handler=cmd_run)

    for name, handler, text in (("status", cmd_status, "상태를 한 번 찍습니다"),
                                ("finish", cmd_finish, "전환 후 정리: 복제 자원, VPN, 비밀값"),
                                ("stop", cmd_stop, "전환 전 중단과 정리: 복제 자원, VPN, 비밀값")):
        command = commands.add_parser(name, help=text)
        command.add_argument("run_id")
        command.set_defaults(handler=handler)

    cutover = commands.add_parser("cutover", help="쓰기를 목적지로 전환합니다")
    cutover.add_argument("run_id")
    cutover.add_argument("--by", required=True, help="승인자")
    cutover.set_defaults(handler=cmd_cutover)
    return parser


def main(argv: list[str] | None = None, *, agent: CdcAgentClient | None = None, out: TextIO = sys.stdout,
         sleep: Callable[[float], None] = time.sleep) -> int:
    args = _parser().parse_args(argv)
    viewer = Viewer(out, getattr(args, "run_id", None))
    with attached(viewer):
        try:
            client = agent or CdcAgentClient(AgentSettings.from_env())
            return args.handler(args, viewer, client, sleep)
        except KeyboardInterrupt:
            viewer.emit("안내", "화면을 닫습니다. 복제는 에이전트에서 계속됩니다. VPN 작업 중이었다면 stop으로 정리하세요.")
            return 130
        except _EXPECTED_ERRORS as exc:
            # JSON 오류 문구와 pydantic 원문이 입력값을 인용하지 않도록 형식 오류는 종류만 남긴다.
            detail = str(exc) if not isinstance(exc, json.JSONDecodeError) else "입력 파일이 JSON이 아닙니다"
            viewer.emit("오류", f"{type(exc).__name__}: {detail}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
