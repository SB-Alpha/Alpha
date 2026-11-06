import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.features.db_migration.client import AgentError
from app.features.workflows.context import with_stage
from app.features.workflows.engine import Engine
from app.features.workflows.runners import (
    _private_ip_metric,
    clean_up_cdc,
    DISCOVERY_MERGED_MESSAGE,
    RUNNERS,
    MissingStageOutput,
    run_app_deploy,
    run_assessment,
    run_cutover,
    run_db_migration,
    run_provisioning,
)
from app.features.workflows.schemas import (
    MigrationRequest,
    StageName,
    WorkflowState,
)
from app.features.workflows.store import RunStore

REQUEST = MigrationRequest(
    github_repo_url="https://github.com/example/target-app",
    aws_region="ap-northeast-2",
    target_region="asia-northeast3",
    aws_credentials_ref="secret://mig-001/source-cloud",
    db_admin_ref="secret://mig-001/source-db-admin",
)


@pytest.fixture(autouse=True)
def no_real_secret_store(monkeypatch):
    """테스트가 실제 시크릿 DB 를 만들거나 읽지 않게 합니다."""
    monkeypatch.setattr("app.core.secrets.has_secret", lambda ref: False)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(RunStore(tmp_path / "workflows.sqlite3"), RUNNERS)


@pytest.fixture
def fake_assessment(monkeypatch):
    """#27 이 합쳐지기 전에도 연결 코드만 확인하도록 assessment 모듈을 대신합니다."""
    calls = []

    def run(inp, ctx):
        calls.append(inp)
        ctx.report("running", 100, None, "계획 생성 완료")
        return {"source": {}, "plan": {"target_infra": {"region": inp.target_region}}, "gaps": []}

    package = ModuleType("app.features.assessment")
    package.service = SimpleNamespace(run=run)
    schemas = ModuleType("app.features.assessment.schemas")
    schemas.AssessmentInput = lambda **fields: SimpleNamespace(**fields)
    monkeypatch.setitem(sys.modules, "app.features.assessment", package)
    monkeypatch.setitem(sys.modules, "app.features.assessment.schemas", schemas)
    return SimpleNamespace(calls=calls, package=package)


def test_assessment_runs_and_waits_for_plan_approval(engine, fake_assessment):
    run = engine.advance(engine.create_run(REQUEST).run_id)
    run_id = run.run_id

    assert run.state == WorkflowState.WAITING_APPROVAL
    assert run.waiting_gate == "plan"
    planning = run.stage(StageName.PLANNING)
    assert planning.state == WorkflowState.SUCCEEDED
    assert planning.manual is False
    assert run.outputs["planning"]["plan"]["target_infra"]["region"] == "asia-northeast3"
    inp = fake_assessment.calls[0]
    assert inp.run_id == run_id
    assert inp.aws_credentials_ref == REQUEST.aws_credentials_ref
    assert inp.db_admin_ref == REQUEST.db_admin_ref


def test_missing_credentials_ref_stops_without_calling_assessment(engine, fake_assessment):
    request = REQUEST.model_copy(update={"aws_credentials_ref": None})

    with pytest.raises(MissingStageOutput, match="원본 AWS 키 이름표"):
        run_assessment(engine.create_run(request), ctx=None)  # type: ignore[arg-type]

    assert fake_assessment.calls == []


def test_import_error_inside_assessment_run_is_not_hidden(engine, fake_assessment):
    def broken(inp, ctx):
        raise ImportError("boto3")

    fake_assessment.package.service = SimpleNamespace(run=broken)

    with pytest.raises(ImportError, match="boto3"):
        run_assessment(engine.create_run(REQUEST), ctx=None)  # type: ignore[arg-type]


def test_absent_assessment_module_gives_clear_error(engine, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.features.assessment", None)

    with pytest.raises(RuntimeError, match="assessment 모듈을 찾을 수 없습니다"):
        run_assessment(engine.create_run(REQUEST), ctx=None)  # type: ignore[arg-type]


def test_discovery_passes_through_as_merged_into_assessment(engine, fake_assessment):
    run = engine.advance(engine.create_run(REQUEST).run_id)

    discovery = run.stage(StageName.DISCOVERY)
    assert discovery.state == WorkflowState.SUCCEEDED
    assert discovery.manual is False
    assert "assessment 에 통합" in discovery.message
    assert run.outputs["discovery"] == {"merged_into": "planning", "note": DISCOVERY_MERGED_MESSAGE}
    # 원본 조회는 planning(assessment) 한 번만
    assert len(fake_assessment.calls) == 1


def test_provisioning_runner_calls_service(engine, monkeypatch):
    calls = []

    def fake_run(inp, ctx):
        calls.append(inp)
        return {"applied": True, "target": {"db": "gcp"}}

    monkeypatch.setattr("app.features.provisioning.service.run", fake_run, raising=False)

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"outputs": {"planning": {"plan": {"target_infra": {"region": "asia-east1"}}}}})

    out = run_provisioning(run, ctx=None)  # type: ignore[arg-type]
    assert out == {"applied": True, "target": {"db": "gcp"}}
    assert calls[0].run_id == run.run_id
    assert calls[0].target_infra == {"region": "asia-east1"}


def test_provisioning_runner_missing_plan_raises(engine):
    run = engine.create_run(REQUEST)
    with pytest.raises(MissingStageOutput, match="planning 단계 출력이 없습니다"):
        run_provisioning(run, ctx=None)  # type: ignore[arg-type]


def test_db_migration_runner_calls_service(engine, monkeypatch):
    calls = []

    def fake_run(inp, ctx):
        calls.append(inp)
        return {"state": "streaming", "job_id": "job-1"}

    monkeypatch.setattr("app.features.db_migration.service.run", fake_run, raising=False)

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"outputs": {
        "planning": {"source": {"account_id": "123", "tables": ["public.users", "alpha_cdc.cutover_marker", "alpha_cdc.foo"]}},
        "provisioning": {"target": {"project_id": "p-1"}},
    }})

    out = run_db_migration(run, ctx=None)  # type: ignore[arg-type]
    assert out == {"state": "streaming", "job_id": "job-1"}
    assert calls[0]["run_id"] == run.run_id
    assert calls[0]["source"]["account_id"] == "123"
    assert calls[0]["source"]["tables"] == ["public.users"]
    assert calls[0]["target"] == {"project_id": "p-1"}


def test_db_migration_runner_detects_streaming_in_background(engine, monkeypatch):
    import threading

    stop_event = threading.Event()

    def fake_streaming_run(inp, proxy_ctx):
        # 스트리밍 도달 보고 후 백그라운드에서 계속 대기
        proxy_ctx.report("running", 50, {"phase": "streaming", "copied_rows": 100}, "CDC 스트리밍 중")
        stop_event.wait(timeout=2.0)
        return {"state": "stopped", "job_id": inp["run_id"]}

    monkeypatch.setattr("app.features.db_migration.service.run", fake_streaming_run, raising=False)

    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append((state, progress, metrics, message))

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"outputs": {
        "planning": {"source": {"tables": ["public.users"]}},
        "provisioning": {"target": {"project_id": "p-1"}},
    }})

    out = run_db_migration(run, ctx=FakeCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]
    stop_event.set()

    assert out["state"] == "streaming"
    assert out["phase"] == "streaming"
    assert out["metrics"]["copied_rows"] == 100
    assert any("CDC 스트리밍 복제 중" in r[3] for r in reports if r[3])


def test_db_migration_runner_raises_on_background_error(engine, monkeypatch):
    def broken_run(inp, proxy_ctx):
        raise RuntimeError("VPN 연결 실패")

    monkeypatch.setattr("app.features.db_migration.service.run", broken_run, raising=False)

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"outputs": {
        "planning": {"source": {"tables": ["public.users"]}},
        "provisioning": {"target": {"project_id": "p-1"}},
    }})

    with pytest.raises(RuntimeError, match="VPN 연결 실패"):
        run_db_migration(run, ctx=None, poll_interval_s=0.01)  # type: ignore[arg-type]



def test_db_migration_runner_missing_outputs_raises(engine):
    run = engine.create_run(REQUEST)
    with pytest.raises(MissingStageOutput, match="원본"):
        run_db_migration(run, ctx=None)  # type: ignore[arg-type]

    run = run.model_copy(update={"outputs": {"planning": {"source": {"a": 1}}}})
    with pytest.raises(MissingStageOutput, match="대상"):
        run_db_migration(run, ctx=None)  # type: ignore[arg-type]


def test_app_deploy_runner_calls_service(engine, monkeypatch):
    calls = []

    def fake_deploy(inp, ctx):
        calls.append(inp)
        return {"url": "https://app.run.app", "healthy": True}

    monkeypatch.setattr("app.features.provisioning.service.deploy_app", fake_deploy, raising=False)

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"outputs": {"planning": {"plan": {"target_infra": {"service_name": "app"}}}}})

    out = run_app_deploy(run, ctx=None)  # type: ignore[arg-type]
    assert out == {"url": "https://app.run.app", "healthy": True}
    assert calls[0].run_id == run.run_id
    assert calls[0].target_infra == {"service_name": "app"}


def test_cutover_runner_missing_env_raises(engine, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    run = engine.create_run(REQUEST)

    class FakeCtx:
        def report(self, *args, **kwargs):
            pass

    with pytest.raises(RuntimeError, match="CDC_AGENT_URL"):
        run_cutover(run, ctx=FakeCtx())  # type: ignore[arg-type]


def test_cutover_runner_waits_for_stabilizing_and_succeeds(engine, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    calls = []
    poll_counts = [0]

    class FakeAgent:
        def __init__(self, settings):
            self.settings = settings

        def cutover(self, job_id, approved_by):
            calls.append((job_id, approved_by))
            return {"accepted": True}

        def get_job(self, job_id):
            poll_counts[0] += 1
            if poll_counts[0] == 1:
                return {"state": "running", "phase": "cutover", "progress": 75}
            return {
                "state": "running",
                "phase": "stabilizing",
                "progress": 90,
                "metrics": {"traffic_mode": "TARGET"},
                "message": "안정화 관찰 중",
            }

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    run = engine.create_run(REQUEST)
    run = run.model_copy(update={"message": "전환 승인 ② 완료: 박준서"})
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append((state, progress, message))

    out = run_cutover(run, ctx=FakeCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]
    assert out["phase"] == "stabilizing"
    assert calls == [(run.run_id.replace("-", "_"), "박준서")]
    assert reports[-1][1] == 100
    assert "트래픽 전환 완료" in reports[-1][2]


def test_cutover_runner_raises_on_rollback(engine, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class FakeAgent:
        def __init__(self, settings):
            self.settings = settings

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return {
                "state": "waiting_approval",
                "phase": "cutover_ready",
                "message": "전환 실패, 원본으로 재개: marker timeout",
            }

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    run = engine.create_run(REQUEST)

    class FakeCtx:
        def report(self, *args, **kwargs):
            pass

    with pytest.raises(RuntimeError, match="컷오버 실패로 원본으로 롤백되었습니다"):
        run_cutover(run, ctx=FakeCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]


def test_cutover_runner_raises_on_failed(engine, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class FakeAgent:
        def __init__(self, settings):
            self.settings = settings

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return {
                "state": "failed",
                "phase": "cutover",
                "message": "목적지 전환 후 네트워크 에러",
            }

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    run = engine.create_run(REQUEST)

    class FakeCtx:
        def report(self, *args, **kwargs):
            pass

    with pytest.raises(RuntimeError, match="컷오버 실패 \\(failed\\)"):
        run_cutover(run, ctx=FakeCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]


def test_cutover_runner_with_simulation_fallback(engine, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")

    run = engine.create_run(REQUEST)
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append((state, progress, metrics, message))

    job = run_cutover(run, ctx=FakeCtx(), poll_interval_s=0.01)
    assert job["state"] == "succeeded"
    assert job["phase"] == "stabilizing"
    last_report = reports[-1]
    assert last_report[1] == 100
    assert last_report[2]["simulated"] is True
    assert last_report[2]["serving"] == "gcp"


def _run_cutover_with_agent_metrics(engine, monkeypatch, metrics):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class FakeAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return {"state": "running", "phase": "stabilizing", "metrics": metrics, "message": "완료"}

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append(metrics)

    run_cutover(engine.create_run(REQUEST), ctx=FakeCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]
    return reports[-1]


def test_cutover_metrics_come_from_agent_report(engine, monkeypatch):
    m = _run_cutover_with_agent_metrics(engine, monkeypatch, {"traffic_mode": "TARGET", "cutover_ms": 1250.0})
    assert m["serving"] == "gcp"
    assert m["cutover_seconds"] == 1.25


def test_cutover_does_not_invent_metrics_when_agent_reports_none(engine, monkeypatch):
    m = _run_cutover_with_agent_metrics(engine, monkeypatch, {})
    assert not {"serving", "failed_requests", "cutover_seconds"} & set(m)


def _planning_metrics(engine, request):
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append(metrics or {})

    run_assessment(engine.create_run(request), ctx=FakeCtx())  # type: ignore[arg-type]
    return {k: v for m in reports for k, v in m.items()}


def test_planning_reports_encryption_only_for_secrets_in_the_store(engine, fake_assessment, monkeypatch):
    monkeypatch.setattr("app.core.secrets.has_secret", lambda ref: True)
    metrics = _planning_metrics(engine, REQUEST)
    assert metrics["secret_encryption"] == "ENCRYPTED"
    assert "iam_audit_status" not in metrics


def test_planning_omits_encryption_when_a_secret_is_not_in_the_store(engine, fake_assessment, monkeypatch):
    monkeypatch.setattr("app.core.secrets.has_secret", lambda ref: ref != REQUEST.db_admin_ref)
    assert "secret_encryption" not in _planning_metrics(engine, REQUEST)


def test_planning_omits_encryption_when_the_store_cannot_be_checked(engine, fake_assessment, monkeypatch):
    def unavailable(ref):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.core.secrets.has_secret", unavailable)
    assert "secret_encryption" not in _planning_metrics(engine, REQUEST)


def test_planning_omits_encryption_when_a_ref_is_not_a_secret_ref(engine, fake_assessment, monkeypatch):
    def must_not_be_called(ref):
        raise AssertionError(ref)

    monkeypatch.setattr("app.core.secrets.has_secret", must_not_be_called)
    request = REQUEST.model_copy(update={"aws_credentials_ref": "profile:default"})
    assert "secret_encryption" not in _planning_metrics(engine, request)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"target": {"db": {"private_ip": "10.31.0.5"}}}, {"cloud_sql_private_ip": "SECURE"}),
        ({"target": {"db": {"private_ip": "34.64.0.1"}}}, {"cloud_sql_private_ip": "PUBLIC"}),
        (SimpleNamespace(target={"db": {"private_ip": "10.31.0.5"}}), {"cloud_sql_private_ip": "SECURE"}),
        ({"target": {"db": {}}}, {}),
        ({"target": {"db": {"private_ip": "not-an-ip"}}}, {}),
        ({}, {}),
    ],
)
def test_private_ip_metric_checks_the_returned_address(output, expected):
    assert _private_ip_metric(output) == expected


def _run_db_migration_capturing_input(engine, monkeypatch, ctx):
    seen = {}

    def fake_run(inp, proxy):
        seen.update(inp)
        return {"state": "stopped"}

    monkeypatch.setattr("app.features.db_migration.service.run", fake_run)
    run = engine.create_run(REQUEST).model_copy(
        update={"outputs": {"planning": {"source": {"tables": []}}, "provisioning": {"target": {"db": {}}}}}
    )
    run_db_migration(run, ctx=ctx, poll_interval_s=0.01)  # type: ignore[arg-type]
    return seen


def test_db_migration_skips_real_vpn_and_marks_run_in_simulation(engine, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append(metrics or {})

    seen = _run_db_migration_capturing_input(engine, monkeypatch, FakeCtx())

    assert seen["options"] == {"skip_vpn": True}
    assert reports[0]["simulated"] is True


def test_db_migration_uses_real_vpn_path_with_real_agent_url(engine, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "https://cdc.example")
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")

    seen = _run_db_migration_capturing_input(engine, monkeypatch, None)

    assert "options" not in seen



def test_cutover_reports_probe_measurements_when_configured(engine, monkeypatch):
    monkeypatch.setenv("ALPHA_PROBE_URL", "http://127.0.0.1:9/")
    m = _run_cutover_with_agent_metrics(engine, monkeypatch, {"traffic_mode": "TARGET"})
    assert m["failed_requests"] >= 1
    assert m["failed_requests"] == m["probe_requests"]


def test_simulated_cutover_does_not_probe(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    monkeypatch.setenv("ALPHA_PROBE_URL", "http://127.0.0.1:9/")
    monkeypatch.setattr("app.features.workflows.runners.prober_from_env", lambda: pytest.fail("프로버를 켜면 안 됩니다"))
    monkeypatch.setattr("app.features.workflows.runners._request_cutover", lambda *a: {"phase": "stabilizing"})
    assert run_cutover(None, ctx=None)["phase"] == "stabilizing"  # type: ignore[arg-type]


def _run_with_cdc_stage(engine, *, state=WorkflowState.SUCCEEDED, manual=False):
    run = engine.create_run(REQUEST)
    return with_stage(run, StageName.DB_MIGRATION, state=state, manual=manual)


class _FakeCleanupAgent:
    def __init__(self, job=None, error=None):
        self._job = job
        self._error = error

    def get_job(self, job_id):
        if self._error:
            raise self._error
        return self._job


def _patch_cleanup(monkeypatch, agent):
    stopped = []
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", lambda settings: agent)
    monkeypatch.setattr(
        "app.features.db_migration.service.stop",
        lambda run_id, *, agent: stopped.append(run_id) or {"run_id": run_id},
    )
    return stopped


@pytest.mark.parametrize("state", [WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.ABORTED])
def test_clean_up_cdc_stops_a_job_that_is_still_before_the_cutover(engine, monkeypatch, state):
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent({"phase": "cutover_ready", "state": "failed"}))
    run = _run_with_cdc_stage(engine, state=state)

    assert clean_up_cdc(run) is True
    assert stopped == [run.run_id]


def test_clean_up_cdc_still_stops_when_the_agent_has_no_job(engine, monkeypatch):
    # 작업 등록 전에 중단돼도 VPN 은 만들어졌을 수 있습니다.
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent(error=AgentError(404, "not found")))
    run = _run_with_cdc_stage(engine, state=WorkflowState.ABORTED)

    assert clean_up_cdc(run) is True
    assert stopped == [run.run_id]


@pytest.mark.parametrize("change", [{"state": WorkflowState.PENDING}, {"manual": True}])
def test_clean_up_cdc_does_nothing_when_cdc_never_ran(engine, monkeypatch, change):
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent({"phase": "streaming", "state": "running"}))

    assert clean_up_cdc(_run_with_cdc_stage(engine, **change)) is False
    assert stopped == []


def test_clean_up_cdc_does_nothing_without_an_agent(engine, monkeypatch):
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent())
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)

    assert clean_up_cdc(_run_with_cdc_stage(engine)) is False
    assert stopped == []


@pytest.mark.parametrize("phase", ["cutover", "stabilizing", "closed"])
@pytest.mark.parametrize("job_state", ["running", "failed"])
def test_clean_up_cdc_refuses_a_job_past_the_cutover(engine, monkeypatch, phase, job_state):
    # 실패한 전환 뒤 작업을 stop 하면 VPN 이 지워져 원본 복제 슬롯을 지울 길이 끊깁니다.
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent({"phase": phase, "state": job_state}))

    with pytest.raises(RuntimeError, match="전환"):
        clean_up_cdc(_run_with_cdc_stage(engine))
    assert stopped == []


def test_clean_up_cdc_passes_on_an_agent_read_failure(engine, monkeypatch):
    stopped = _patch_cleanup(monkeypatch, _FakeCleanupAgent(error=AgentError(502, "unavailable")))

    with pytest.raises(AgentError):
        clean_up_cdc(_run_with_cdc_stage(engine))
    assert stopped == []


WAIT_S = 5


def _engine_through_cdc(tmp_path: Path, monkeypatch, cdc, app_deploy) -> Engine:
    """실제 Engine 과 run_db_migration 을 쓰고, 클라우드에 닿는 단계만 바꿉니다."""
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)
    monkeypatch.setattr("app.features.db_migration.service.run", cdc, raising=False)
    return Engine(
        RunStore(tmp_path / "pipeline.sqlite3"),
        {
            StageName.DISCOVERY: lambda run, ctx: {},
            StageName.PLANNING: lambda run, ctx: {"source": {"tables": ["public.users"]}, "plan": {}},
            StageName.PROVISIONING: lambda run, ctx: {"target": {"project_id": "p-1"}},
            StageName.DB_MIGRATION: lambda run, ctx: run_db_migration(run, ctx, poll_interval_s=0.01),
            StageName.APP_DEPLOY: app_deploy,
            StageName.CUTOVER: lambda run, ctx: {},
        },
    )


def _advance_past_plan_approval(engine: Engine) -> str:
    run_id = engine.create_run(REQUEST).run_id
    engine.advance(run_id)
    engine.approve(run_id, "plan")
    return run_id


def test_pipeline_reaches_cutover_gate_while_cdc_keeps_reporting(tmp_path, monkeypatch):
    next_stage_started = threading.Event()
    reported_after_finish = threading.Event()
    run_finished = threading.Event()

    def cdc(inp, proxy):
        proxy.report("running", 50, {"phase": "streaming", "replication_lag_bytes": 10}, "복제 중")
        assert next_stage_started.wait(WAIT_S)
        proxy.report("running", 60, {"phase": "streaming", "replication_lag_bytes": 3}, "복제 중")
        reported_after_finish.set()
        run_finished.wait(WAIT_S)
        return {"state": "stopped"}

    def app_deploy(run, ctx):
        next_stage_started.set()
        assert reported_after_finish.wait(WAIT_S)
        return {"healthy": True}

    engine = _engine_through_cdc(tmp_path, monkeypatch, cdc, app_deploy)
    run_id = _advance_past_plan_approval(engine)

    final = engine.advance(run_id)
    run_finished.set()

    assert final.state == WorkflowState.WAITING_APPROVAL
    assert final.waiting_gate == "cutover"
    stage = final.stage(StageName.DB_MIGRATION)
    assert stage.state == WorkflowState.SUCCEEDED
    assert stage.metrics["replication_lag_bytes"] == 3


def _ends_with_failed_result():
    return SimpleNamespace(state="failed", message="커넥터 실패")


def _ends_with_stopped_result():
    return SimpleNamespace(state="stopped", message=None)


def _ends_with_exception():
    raise RuntimeError("커넥터 실패")


@pytest.mark.parametrize(
    "end_cdc",
    [_ends_with_failed_result, _ends_with_stopped_result, _ends_with_exception],
)
def test_cdc_ending_after_stage_finished_is_reported_without_changing_run(tmp_path, monkeypatch, end_cdc):
    next_stage_started = threading.Event()

    def cdc(inp, proxy):
        proxy.report("running", 50, {"phase": "streaming"}, "복제 중")
        assert next_stage_started.wait(WAIT_S)
        return end_cdc()

    def app_deploy(run, ctx):
        next_stage_started.set()
        return {"healthy": True}

    engine = _engine_through_cdc(tmp_path, monkeypatch, cdc, app_deploy)
    run_id = _advance_past_plan_approval(engine)
    final = engine.advance(run_id)

    deadline = time.monotonic() + WAIT_S
    stage = final.stage(StageName.DB_MIGRATION)
    while "cdc_error" not in stage.metrics and time.monotonic() < deadline:
        time.sleep(0.01)
        stage = engine.get(run_id).stage(StageName.DB_MIGRATION)

    assert stage.metrics["cdc_error"]
    assert stage.state == WorkflowState.SUCCEEDED
    assert engine.get(run_id).state == WorkflowState.WAITING_APPROVAL


def test_cdc_failure_before_streaming_fails_the_stage_without_cdc_error(tmp_path, monkeypatch):
    def cdc(inp, proxy):
        return SimpleNamespace(state="failed", message="접속 실패")

    engine = _engine_through_cdc(tmp_path, monkeypatch, cdc, lambda run, ctx: {})
    run_id = _advance_past_plan_approval(engine)

    final = engine.advance(run_id)

    assert final.state == WorkflowState.FAILED
    stage = final.stage(StageName.DB_MIGRATION)
    assert "접속 실패" in stage.message
    assert "cdc_error" not in stage.metrics
