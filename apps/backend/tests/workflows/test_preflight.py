import logging
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tests.fakes.cdc_agent import FakeCdcAgent
from app.features.provisioning.terraform import ProvisioningError
from app.features.workflows import preflight
from app.features.workflows.preflight import _http_status, evaluate_cutover
from app.features.workflows.schemas import (
    STAGE_ORDER,
    MigrationRequest,
    PreflightCheck,
    RunStatus,
    StageName,
    StageStatus,
    WorkflowState,
)

APP_URL = "https://target-app.run.app"
CHECK_IDS = ["job_ready", "db_connectivity", "data_match", "replication_lag", "target_ready", "worker_target"]
ENV_VARS = (
    "CDC_AGENT_URL",
    "ALPHA_CDC_SIMULATION",
    "ALPHA_CUTOVER_MAX_LAG_BYTES",
    "WORKER_URL",
    "WORKER_TOKEN",
    "WORKER_TOKEN_FILE",
)
READY_JOB = {
    "state": "waiting_approval",
    "phase": "cutover_ready",
    "metrics": {"measured_at": "2026-11-06T01:00:00Z", "mismatch": 0, "replication_lag_bytes": 1024},
}
SECRET_TEXT = "connection to db.internal.example:5432 failed password=hunter2 token=abc"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_run(outputs=None, manual: bool = False) -> RunStatus:
    ts = datetime.now(timezone.utc)
    stages = [StageStatus(stage=s, manual=manual and s == StageName.APP_DEPLOY) for s in STAGE_ORDER]
    return RunStatus(
        run_id="mig-20261106-abcd1234",
        state=WorkflowState.WAITING_APPROVAL,
        current_stage=StageName.CUTOVER,
        waiting_gate="cutover",
        request=MigrationRequest(github_repo_url="https://github.com/example/app", aws_region="ap-northeast-2"),
        stages=stages,
        outputs={"app_deploy": {"app_url": APP_URL}} if outputs is None else outputs,
        created_at=ts,
        updated_at=ts,
    )


def worker_at(url):
    return lambda: SimpleNamespace(get_target=lambda: url)


def evaluate(run=None, job=READY_JOB, http_get=None, worker=None, **kwargs):
    return evaluate_cutover(
        run or make_run(),
        get_job=lambda job_id: job,
        http_get=http_get or (lambda url: 200),
        worker_target=worker or worker_at(APP_URL),
        **kwargs,
    )


def check(report, check_id) -> PreflightCheck:
    return next(c for c in report.checks if c.id == check_id)


def job_with(**metrics):
    return {**READY_JOB, "metrics": {**READY_JOB["metrics"], **metrics}}


def job_without(*names):
    return {**READY_JOB, "metrics": {k: v for k, v in READY_JOB["metrics"].items() if k not in names}}


def raising(error):
    def call(*args, **kwargs):
        raise error

    return call


# -- 보고서 전체 ------------------------------------------------------------------


def test_report_shape_and_check_order():
    report = evaluate()

    assert report.run_id == "mig-20261106-abcd1234"
    assert report.gate == "cutover"
    assert report.advisory is True
    assert "simulated" not in report.model_dump()
    assert [c.id for c in report.checks] == CHECK_IDS
    assert all(c.label for c in report.checks)
    assert report.measured_at.tzinfo is not None


def test_all_pass_with_limit_is_go():
    report = evaluate(max_lag_bytes=2048)
    assert {c.status for c in report.checks} == {"pass"}
    assert report.verdict == "go"


def test_info_lag_does_not_affect_verdict():
    report = evaluate()
    assert check(report, "replication_lag").status == "info"
    assert report.verdict == "go"


def test_any_fail_gives_no_go_even_with_unknowns():
    report = evaluate(job=job_with(mismatch=3), worker=raising(ProvisioningError("x")))
    assert check(report, "data_match").status == "fail"
    assert check(report, "worker_target").status == "unknown"
    assert report.verdict == "no_go"


def test_unknown_without_fail_gives_unverified():
    report = evaluate(worker=raising(ProvisioningError("x")))
    assert report.verdict == "unverified"


@pytest.mark.parametrize(
    "agent_env",
    [{"ALPHA_CDC_SIMULATION": "1"}, {"CDC_AGENT_URL": "mock://simulation"}],
    ids=["simulation-flag", "mock-url"],
)
def test_mock_agent_through_real_defaults_never_claims_go(monkeypatch, agent_env):
    # 모의 에이전트는 실제 전환 준비를 보여 주지 못합니다. 그 값으로 점검하지 않고 연결 실패처럼 unknown 으로 둡니다.
    for name, value in agent_env.items():
        monkeypatch.setenv(name, value)
    report = evaluate_cutover(make_run(), http_get=lambda url: 200, worker_target=worker_at(APP_URL), max_lag_bytes=2048)
    for check_id in CHECK_IDS[:4]:
        assert check(report, check_id).status == "unknown"
    assert report.verdict == "unverified"


def test_real_agent_url_is_read_even_with_simulation_flag(monkeypatch):
    fake = FakeCdcAgent().install(monkeypatch)
    fake.jobs["mig_20261106_abcd1234"] = dict(READY_JOB)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")

    report = evaluate_cutover(make_run(), http_get=lambda url: 200, worker_target=worker_at(APP_URL), max_lag_bytes=2048)

    assert ("GET", "/jobs/mig_20261106_abcd1234") in fake.requests
    assert {c.status for c in report.checks} == {"pass"}
    assert report.verdict == "go"


def test_unconfigured_agent_makes_agent_checks_unknown():
    report = evaluate_cutover(make_run(), http_get=lambda url: 200, worker_target=worker_at(APP_URL))
    for check_id in CHECK_IDS[:4]:
        assert check(report, check_id).status == "unknown"
    assert check(report, "target_ready").status == "pass"
    assert report.verdict == "unverified"


def test_default_get_job_uses_the_agent_job_id(monkeypatch):
    seen = []

    class FakeAgent:
        def __init__(self, settings):
            pass

        def get_job(self, job_id):
            seen.append(job_id)
            return READY_JOB

    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    report = evaluate_cutover(make_run(), http_get=lambda url: 200, worker_target=worker_at(APP_URL))

    assert seen == ["mig_20261106_abcd1234"]
    assert check(report, "job_ready").status == "pass"


def test_exception_text_never_reaches_report_or_log(caplog):
    caplog.set_level(logging.DEBUG)
    error = RuntimeError(SECRET_TEXT)

    report = evaluate_cutover(
        make_run(),
        get_job=raising(error),
        http_get=raising(error),
        worker_target=raising(error),
    )

    assert {c.status for c in report.checks} == {"unknown"}
    assert all(c.detail for c in report.checks)
    assert "hunter2" not in report.model_dump_json()
    assert "db.internal" not in report.model_dump_json()
    assert "hunter2" not in caplog.text
    assert "db.internal" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert not any(r.exc_info for r in caplog.records)


def test_one_broken_check_does_not_stop_the_others():
    report = evaluate(http_get=raising(OSError("down")))
    assert check(report, "target_ready").status == "unknown"
    assert check(report, "worker_target").status == "pass"
    assert check(report, "data_match").status == "pass"


# -- job_ready --------------------------------------------------------------------


def test_job_ready_passes_only_at_cutover_ready():
    assert check(evaluate(), "job_ready").status == "pass"


@pytest.mark.parametrize(
    ("state", "phase"),
    [("running", "streaming"), ("waiting_approval", "streaming"), ("running", "cutover_ready"), ("failed", "cutover")],
)
def test_job_ready_fails_with_state_and_phase_in_detail(state, phase):
    job = {**READY_JOB, "state": state, "phase": phase, "message": SECRET_TEXT}
    result = check(evaluate(job=job), "job_ready")
    assert result.status == "fail"
    assert state in result.detail
    assert phase in result.detail
    assert "hunter2" not in result.detail


@pytest.mark.parametrize("job", [None, "bad", ["bad"]])
def test_malformed_job_makes_agent_checks_unknown(job):
    report = evaluate(job=job)
    for check_id in CHECK_IDS[:4]:
        assert check(report, check_id).status == "unknown"


# -- db_connectivity --------------------------------------------------------------


@pytest.mark.parametrize("measure_error", [SECRET_TEXT, ""])
def test_measure_error_fails_connectivity_without_echoing_it(measure_error):
    report = evaluate(job=job_with(measure_error=measure_error))
    assert check(report, "db_connectivity").status == "fail"
    assert "hunter2" not in report.model_dump_json()
    assert "db.internal" not in report.model_dump_json()


def test_measured_at_passes_connectivity():
    assert check(evaluate(), "db_connectivity").status == "pass"


@pytest.mark.parametrize(
    "measured_at", ["2026-11-06T01:00:00Z", "2026-11-05T03:00:07.000+00:00", "2020-01-01T00:00:00+09:00"]
)
def test_timezone_aware_timestamp_of_any_age_passes_connectivity(measured_at):
    assert check(evaluate(job=job_with(measured_at=measured_at)), "db_connectivity").status == "pass"


@pytest.mark.parametrize("job", [job_without("measured_at"), job_with(measured_at=None), {**READY_JOB, "metrics": "bad"}])
def test_connectivity_is_unknown_without_a_measurement(job):
    assert check(evaluate(job=job), "db_connectivity").status == "unknown"


@pytest.mark.parametrize(
    "measured_at",
    ["2026-11-06T01:00:00", "2026-11-06", "어제", "", "   ", 1762390800, 1762390800.5, True, ["2026-11-06T01:00:00Z"]],
)
def test_connectivity_is_unknown_when_measured_at_is_not_an_aware_timestamp(measured_at):
    result = check(evaluate(job=job_with(measured_at=measured_at)), "db_connectivity")
    assert result.status == "unknown"
    assert result.detail


# -- data_match -------------------------------------------------------------------


@pytest.mark.parametrize("mismatch", [0, 0.0])
def test_zero_mismatch_passes(mismatch):
    assert check(evaluate(job=job_with(mismatch=mismatch)), "data_match").status == "pass"


def test_positive_mismatch_fails_with_value():
    result = check(evaluate(job=job_with(mismatch=4)), "data_match")
    assert (result.status, result.value) == ("fail", 4)


@pytest.mark.parametrize("job", [job_without("mismatch"), job_with(mismatch=True), job_with(mismatch="0"), job_with(mismatch=-1), job_with(mismatch=1.5)])
def test_missing_or_bad_mismatch_is_unknown(job):
    assert check(evaluate(job=job), "data_match").status == "unknown"


def test_measure_error_makes_stale_mismatch_unknown():
    assert check(evaluate(job=job_with(measure_error="x", mismatch=0)), "data_match").status == "unknown"


# -- replication_lag --------------------------------------------------------------


def test_lag_without_limit_is_info_for_reference():
    result = check(evaluate(), "replication_lag")
    assert result.status == "info"
    assert result.value == 1024
    assert "기준" in result.detail


@pytest.mark.parametrize("env", ["", "   "])
def test_blank_limit_env_counts_as_unset(monkeypatch, env):
    monkeypatch.setenv("ALPHA_CUTOVER_MAX_LAG_BYTES", env)
    assert check(evaluate(), "replication_lag").status == "info"


@pytest.mark.parametrize(("limit", "expected"), [("2048", "pass"), ("1024", "pass"), ("1023", "fail"), ("0", "fail")])
def test_lag_is_compared_to_env_limit(monkeypatch, limit, expected):
    monkeypatch.setenv("ALPHA_CUTOVER_MAX_LAG_BYTES", limit)
    result = check(evaluate(), "replication_lag")
    assert (result.status, result.value) == (expected, 1024)


@pytest.mark.parametrize("limit", ["abc", "-1", "1.5", "1e6"])
def test_invalid_limit_env_is_unknown(monkeypatch, limit):
    monkeypatch.setenv("ALPHA_CUTOVER_MAX_LAG_BYTES", limit)
    assert check(evaluate(), "replication_lag").status == "unknown"


def test_explicit_limit_overrides_env(monkeypatch):
    monkeypatch.setenv("ALPHA_CUTOVER_MAX_LAG_BYTES", "abc")
    assert check(evaluate(max_lag_bytes=1), "replication_lag").status == "fail"
    assert check(evaluate(max_lag_bytes=None), "replication_lag").status == "info"


@pytest.mark.parametrize(
    "job",
    [
        job_without("replication_lag_bytes"),
        job_with(replication_lag_bytes=None),
        job_with(replication_lag_bytes="1024"),
        job_with(replication_lag_bytes=True),
        job_with(replication_lag_bytes=1.5),
        job_with(replication_lag_bytes=float("nan")),
        job_with(measure_error="x"),
    ],
)
def test_lag_is_unknown_when_not_measured(job):
    assert check(evaluate(job=job, max_lag_bytes=2048), "replication_lag").status == "unknown"


def test_integral_float_lag_is_reported_as_int():
    result = check(evaluate(job=job_with(replication_lag_bytes=2048.0)), "replication_lag")
    assert result.value == 2048


# -- target_ready -----------------------------------------------------------------


def test_target_ready_calls_the_readiness_path_of_provisioning():
    seen = []

    def http_get(url):
        seen.append(url)
        return 200

    run = make_run({"app_deploy": {"app_url": APP_URL + "/"}})
    assert check(evaluate(run, http_get=http_get), "target_ready").status == "pass"
    assert seen == [APP_URL + "/health/ready"]


@pytest.mark.parametrize("status", [301, 404, 503])
def test_non_200_fails_with_status_as_value(status):
    result = check(evaluate(http_get=lambda url: status), "target_ready")
    assert (result.status, result.value) == ("fail", status)


@pytest.mark.parametrize(
    "outputs",
    [
        {},
        {"app_deploy": {}},
        {"app_deploy": None},
        {"app_deploy": {"app_url": None}},
        {"app_deploy": {"app_url": 5}},
        {"app_deploy": {"app_url": "http://target-app.run.app"}},
        {"app_deploy": {"app_url": "https:///health"}},
        {"app_deploy": {"app_url": "ftp://target-app.run.app"}},
        {"app_deploy": {"app_url": "file:///etc/passwd"}},
        {"app_deploy": {"app_url": "https://user:pw@target-app.run.app"}},
        {"app_deploy": {"app_url": "https://[bad"}},
    ],
)
def test_unsafe_or_missing_url_is_unknown_and_never_called(outputs):
    calls = []
    report = evaluate(make_run(outputs), http_get=lambda url: calls.append(url) or 200)
    assert check(report, "target_ready").status == "unknown"
    assert calls == []


def test_manual_app_deploy_url_is_never_called():
    calls = []
    run = make_run({"app_deploy": {"app_url": "https://169.254.169.254/latest"}}, manual=True)
    report = evaluate(run, http_get=lambda url: calls.append(url) or 200)
    assert check(report, "target_ready").status == "unknown"
    assert calls == []
    assert "169.254" not in report.model_dump_json()


def test_network_error_is_unknown_not_invented_status():
    result = check(evaluate(http_get=raising(TimeoutError())), "target_ready")
    assert (result.status, result.value) == ("unknown", None)


class _Handler(BaseHTTPRequestHandler):
    agents: list[str] = []
    followed: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).agents.append(self.headers.get("User-Agent", ""))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        if self.path == "/landed":
            type(self).followed.append(self.path)
        self.send_response(503 if self.path == "/down" else 200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server(monkeypatch):
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(_Handler, "agents", [])
    monkeypatch.setattr(_Handler, "followed", [])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_helper_returns_status_without_following_redirects(local_server):
    assert _http_status(local_server + "/ok") == 200
    assert _http_status(local_server + "/down") == 503
    assert _http_status(local_server + "/redirect") == 302
    assert _Handler.followed == []


def test_http_helper_sends_non_default_user_agent(local_server):
    _http_status(local_server + "/ok")
    assert _Handler.agents[0]
    assert "Python-urllib" not in _Handler.agents[0]


def test_http_helper_passes_its_timeout(monkeypatch):
    calls = []

    class FakeOpener:
        def open(self, request, timeout=None):
            calls.append(timeout)
            return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(preflight.urllib.request, "build_opener", lambda *handlers: FakeOpener())

    assert _http_status(APP_URL) == 200
    assert calls == [preflight.HTTP_TIMEOUT_S]


def test_http_helper_raises_on_network_error(local_server):
    closed = local_server.rsplit(":", 1)[0] + ":1"
    with pytest.raises(OSError):
        _http_status(closed + "/ok")


# -- worker_target ----------------------------------------------------------------


def test_worker_target_ignores_trailing_slashes():
    run = make_run({"app_deploy": {"app_url": APP_URL + "/"}})
    assert check(evaluate(run, worker=worker_at(APP_URL)), "worker_target").status == "pass"


@pytest.mark.parametrize("stored", ["https://other.run.app"])
def test_different_worker_target_fails_without_urls_in_detail(stored):
    report = evaluate(worker=worker_at(stored))
    assert check(report, "worker_target").status == "fail"
    dumped = check(report, "worker_target").model_dump_json()
    assert "run.app" not in dumped


def test_worker_without_a_stored_target_is_unknown_and_says_it_uses_its_default():
    report = evaluate(worker=worker_at(None))
    result = check(report, "worker_target")
    assert result.status == "unknown"
    assert "기본" in result.detail
    assert "run.app" not in result.model_dump_json()
    assert report.verdict == "unverified"


def test_unconfigured_worker_is_unknown():
    assert check(evaluate(worker=lambda: None), "worker_target").status == "unknown"


def test_default_worker_comes_from_env():
    report = evaluate_cutover(make_run(), get_job=lambda job_id: READY_JOB, http_get=lambda url: 200)
    assert check(report, "worker_target").status == "unknown"


def test_worker_token_missing_is_unknown(monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://gate.example")
    report = evaluate_cutover(make_run(), get_job=lambda job_id: READY_JOB, http_get=lambda url: 200)
    result = check(report, "worker_target")
    assert result.status == "unknown"
    assert "gate.example" not in report.model_dump_json()


def test_worker_error_is_unknown():
    worker = lambda: SimpleNamespace(get_target=raising(ProvisioningError("https://gate.example HTTP 500")))  # noqa: E731
    report = evaluate(worker=worker)
    assert check(report, "worker_target").status == "unknown"
    assert "gate.example" not in report.model_dump_json()


@pytest.mark.parametrize("outputs", [{}, {"app_deploy": {"app_url": None}}, {"app_deploy": {"app_url": 5}}])
def test_worker_target_is_unknown_without_app_url(outputs):
    calls = []

    def get_target():
        calls.append(1)
        return APP_URL

    report = evaluate(make_run(outputs), worker=lambda: SimpleNamespace(get_target=get_target))
    assert check(report, "worker_target").status == "unknown"
    assert calls == []


def test_manual_app_url_is_only_compared_never_called():
    run = make_run({"app_deploy": {"app_url": APP_URL}}, manual=True)
    assert check(evaluate(run), "worker_target").status == "pass"
