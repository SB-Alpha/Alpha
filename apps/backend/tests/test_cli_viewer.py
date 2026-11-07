import io
import json
import subprocess

import pytest

from app.features.db_migration import cli_viewer, db_workflow
from app.features.db_migration.progress_log import ProgressLog
from app.features.db_migration.schemas import CutoverResult, FinishResult, RunStatus
from app.features.db_migration.vpn import OciSettings
from app.features.db_migration.vpn_terraform import TerraformSession

PASSWORD = "do-not-print-this-password"
INPUT = {
    "run_id": "run_1",
    "source": {"cloud": "aws", "region": "ap-northeast-2", "db": {"user": "admin", "password": PASSWORD}},
    "target": {"cloud": "gcp", "region": "asia-northeast3",
               "db": {"user": "admin", "password": PASSWORD, "app_user": "app"}},
}


def status(**fields) -> RunStatus:
    return RunStatus(**{"run_id": "run_1", "job_id": "mig_1", "state": "running", "phase": "vpn", "progress": 0, **fields})


def lines(out: io.StringIO, tag: str) -> list[str]:
    return [line for line in out.getvalue().splitlines() if f"[{tag}]" in line]


@pytest.fixture
def input_file(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(INPUT), encoding="utf-8")
    return str(path)


@pytest.fixture
def statuses(monkeypatch):
    def install(*items):
        queue = list(items)
        monkeypatch.setattr(db_workflow, "status", lambda run_id, agent=None: queue.pop(0) if len(queue) > 1 else queue[0])
    return install


def test_run_prints_procedure_and_ui_values_until_ready(monkeypatch, input_file, statuses):
    def start(body, agent=None, ctx=None):
        assert body["source"]["db"]["password"] == PASSWORD
        ProgressLog("run_1", "mig_1").event("vpn.start")
        ctx.report("running", 1, {"phase": "vpn"}, "DB 접속 확인")
        return "run_1"

    monkeypatch.setattr(db_workflow, "run", start)
    statuses(status(), status(state="waiting_approval", phase="cutover_ready", ready_for_cutover=True))
    out = io.StringIO()

    code = cli_viewer.main(["--interval", "0", "run", input_file], agent=object(), out=out, sleep=lambda _: None)

    assert code == 0
    assert "vpn.start" in lines(out, "절차")[0]
    assert "DB 접속 확인" in lines(out, "UI:report")[0]
    assert len(lines(out, "UI:status")) == 2 and "cutover_ready" in lines(out, "UI:status")[1]
    assert "cutover run_1 --by" in out.getvalue()
    assert PASSWORD not in out.getvalue()


def test_run_failure_returns_1_and_names_the_cleanup(monkeypatch, input_file, statuses):
    monkeypatch.setattr(db_workflow, "run", lambda body, agent=None, ctx=None: "run_1")
    statuses(status(state="failed", message="터널 실패", cleanup_required=True, remaining=["vpn"]))
    out = io.StringIO()

    assert cli_viewer.main(["run", input_file], agent=object(), out=out, sleep=lambda _: None) == 1
    assert "stop run_1" in out.getvalue() and "터널 실패" in out.getvalue()


def test_invalid_input_names_fields_without_echoing_values(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"source": {"db": {"password": PASSWORD}}}), encoding="utf-8")
    out = io.StringIO()

    assert cli_viewer.main(["run", str(path)], agent=object(), out=out) == 1
    assert "[오류]" in out.getvalue() and PASSWORD not in out.getvalue()


def test_broken_json_does_not_quote_the_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"password": "%s"' % PASSWORD, encoding="utf-8")
    out = io.StringIO()

    assert cli_viewer.main(["run", str(path)], agent=object(), out=out) == 1
    assert "JSON" in out.getvalue() and PASSWORD not in out.getvalue()


def test_every_line_is_masked():
    out = io.StringIO()
    viewer = cli_viewer.Viewer(out)
    with cli_viewer.attached(viewer):
        ProgressLog("run_1", "mig_1").event("run.failed", error="postgres://u:hunter22@db password=hunter22")
    viewer.report("failed", 0, None, "token=abcdefgh")

    assert "hunter22" not in out.getvalue() and "abcdefgh" not in out.getvalue()


def test_only_changes_are_printed_and_the_measured_time_is_no_change():
    out = io.StringIO()
    viewer = cli_viewer.Viewer(out, "run_1")
    viewer.report("running", 5, {"copy_percent": 1, "measured_at": "a"}, None)
    viewer.report("running", 5, {"copy_percent": 1, "measured_at": "b"}, None)
    viewer.status(status(metrics={"mismatch": 0, "measured_at": "a"}))
    viewer.status(status(metrics={"mismatch": 0, "measured_at": "b"}))
    viewer.status(status(metrics={"mismatch": 1, "measured_at": "c"}))

    assert len(lines(out, "UI:report")) == 1
    assert len(lines(out, "UI:status")) == 2


def test_logs_go_to_the_view_once_and_settings_are_restored():
    logger = cli_viewer.progress_logger
    before = (logger.level, logger.propagate, list(logger.handlers))
    out = io.StringIO()
    with cli_viewer.attached(cli_viewer.Viewer(out)):
        logger.getChild("db_workflow").warning("run %s failed", "run_1")

    assert len(lines(out, "로그")) == 1 and "WARNING run run_1 failed" in out.getvalue()
    assert (logger.level, logger.propagate, list(logger.handlers)) == before


@pytest.mark.parametrize("outcome,code", [("switched", 0), ("unknown", 1)])
def test_cutover_prints_the_result(monkeypatch, statuses, outcome, code):
    result = CutoverResult(run_id="run_1", job_id="mig_1", outcome=outcome, state="running", phase="stabilizing", message="끝")
    calls = []
    monkeypatch.setattr(db_workflow, "cutover", lambda run_id, by, agent=None: calls.append(by) or result)
    statuses(status())
    out = io.StringIO()

    assert cli_viewer.main(["cutover", "run_1", "--by", "me"], agent=object(), out=out, sleep=lambda _: None) == code
    assert calls == ["me"] and outcome in lines(out, "UI:cutover")[0]


def test_cutover_needs_an_approver():
    with pytest.raises(SystemExit):
        cli_viewer.main(["cutover", "run_1"], agent=object(), out=io.StringIO())


@pytest.mark.parametrize("command,state,code", [("finish", "finished", 0), ("finish", "incomplete", 1),
                                                ("stop", "finished", 0), ("stop", "incomplete", 1)])
def test_cleanup_prints_the_result_and_outlives_the_run_record(monkeypatch, command, state, code):
    result = FinishResult(run_id="run_1", job_id="mig_1", state=state, vpn="removed")
    monkeypatch.setattr(db_workflow, command, lambda run_id, agent=None: result)

    def gone(run_id, agent=None):
        raise db_workflow.UnknownRun(run_id)

    monkeypatch.setattr(db_workflow, "status", gone)
    out = io.StringIO()

    assert cli_viewer.main([command, "run_1"], agent=object(), out=out, sleep=lambda _: None) == code
    assert state in lines(out, f"UI:{command}")[0]


def test_a_failing_call_is_reported(monkeypatch, statuses):
    def refuse(run_id, by, agent=None):
        raise db_workflow.NotAllowed("cutover needs phase cutover_ready")

    monkeypatch.setattr(db_workflow, "cutover", refuse)
    statuses(status())
    out = io.StringIO()

    assert cli_viewer.main(["cutover", "run_1", "--by", "me"], agent=object(), out=out, sleep=lambda _: None) == 1
    assert "cutover needs phase" in lines(out, "오류")[0]


def test_progress_message_becomes_a_procedure_event_once():
    out = io.StringIO()
    db_workflow._runs["run_view"] = db_workflow._Progress(stage="inputs")
    try:
        with cli_viewer.attached(cli_viewer.Viewer(out)):
            db_workflow._set_progress("run_view", message="시작 전 검사 중")
            db_workflow._set_progress("run_view", message="시작 전 검사 중")
            db_workflow._set_progress("run_view", stage="vpn")
    finally:
        db_workflow._runs.pop("run_view")

    assert len(lines(out, "절차")) == 1 and "시작 전 검사 중" in out.getvalue()


@pytest.mark.parametrize("returncode,last", [(0, "terraform.done"), (1, "terraform.failed")])
def test_terraform_steps_are_logged_without_their_values(tmp_path, monkeypatch, returncode, last):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    settings = OciSettings(tmp_path, "r", "DEFAULT", "c", "d", "b", "n", "10.0.0.0/16", "10.0.0.0/24")

    def runner(command, **_):
        return subprocess.CompletedProcess(command, 0 if command[2] == "init" else returncode, "", "")

    tf = TerraformSession(settings, "mig_1", {"oci": "oci-vpn"}, {"oci": lambda: {}}, runner=runner,
                          log=ProgressLog("run_1", "mig_1"))
    out = io.StringIO()
    with cli_viewer.attached(cli_viewer.Viewer(out)):
        try:
            tf.apply("oci", {"shared_secrets": PASSWORD})
        except Exception:
            assert returncode

    events = lines(out, "절차")
    assert "terraform.start" in events[0] and '"role": "oci"' in events[0] and last in events[1]
    assert PASSWORD not in out.getvalue()
