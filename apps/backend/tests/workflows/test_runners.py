import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.features.workflows.engine import Engine
from app.features.workflows.runners import (
    _private_ip_metric,
    _scrub_agent_text,
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
    ApprovalRecord,
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


def _cutover_record(approver, comment=None):
    return ApprovalRecord(gate="cutover", approver=approver, comment=comment, at=datetime.now(timezone.utc))


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
    run = run.model_copy(update={"approval_records": [_cutover_record("박준서")]})
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


class ApproverAgent:
    """cutover 에 넘어온 승인자만 기록합니다. get_job 은 바로 전환 완료를 돌려줍니다."""

    approvers: list[str] = []

    def __init__(self, settings):
        pass

    def cutover(self, job_id, approved_by):
        type(self).approvers.append(approved_by)
        return {"accepted": True}

    def get_job(self, job_id):
        return {"state": "running", "phase": "stabilizing", "metrics": {}}


@pytest.fixture
def approver_agent(monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    monkeypatch.setattr(ApproverAgent, "approvers", [])
    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", ApproverAgent)
    return ApproverAgent


class SilentCtx:
    def report(self, *args, **kwargs):
        pass


def _cutover_engine(tmp_path: Path) -> tuple[Engine, str]:
    """cutover 만 진짜 runner 인 엔진을 전환 승인 대기까지 보냅니다."""

    def stub(run, ctx):
        return {"ok": True}

    runners = {stage: stub for stage in StageName}
    runners[StageName.CUTOVER] = run_cutover
    engine = Engine(RunStore(tmp_path / "approver.sqlite3"), runners)
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    engine.approve(run_id, "plan", approver="계획 승인자")
    assert engine.advance(run_id).waiting_gate == "cutover"
    return engine, run_id


def test_approver_reaches_agent_through_real_engine(tmp_path, approver_agent):
    engine, run_id = _cutover_engine(tmp_path)

    engine.approve(run_id, "cutover", approver="박준서")
    run = engine.advance(run_id)

    assert approver_agent.approvers == ["박준서"]
    assert run.stage(StageName.CUTOVER).state == WorkflowState.SUCCEEDED


def test_comment_is_not_used_as_approver(tmp_path, approver_agent):
    engine, run_id = _cutover_engine(tmp_path)

    engine.approve(run_id, "cutover", "데이터 확인: 이상 없음")
    engine.advance(run_id)

    assert approver_agent.approvers == ["workflows"]


def test_run_message_is_not_parsed_for_approver(engine, approver_agent):
    run = engine.create_run(REQUEST).model_copy(update={"message": "전환 승인 ② 완료: 박준서"})

    run_cutover(run, ctx=SilentCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]

    assert approver_agent.approvers == ["workflows"]


def test_anonymous_reapproval_does_not_credit_the_earlier_approver(engine, approver_agent):
    run = engine.create_run(REQUEST).model_copy(
        update={"approval_records": [_cutover_record("박준서"), _cutover_record(None)]}
    )

    run_cutover(run, ctx=SilentCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]

    assert approver_agent.approvers == ["workflows"]


def test_plan_approver_is_not_used_for_cutover(engine, approver_agent):
    record = ApprovalRecord(gate="plan", approver="계획 승인자", at=datetime.now(timezone.utc))
    run = engine.create_run(REQUEST).model_copy(update={"approval_records": [record]})

    run_cutover(run, ctx=SilentCtx(), poll_interval_s=0.01)  # type: ignore[arg-type]

    assert approver_agent.approvers == ["workflows"]


# -- 전환 시간·순서 검증·단계 증적 ---------------------------------------------------

STEP_KEYS = {"cutover_steps", "cutover_sequence", "cutover_sequence_missing"}
SEQUENCE = ("hold", "marker_on_target", "validated", "switched_to_target", "released")
STABILIZING = {"state": "running", "phase": "stabilizing", "progress": 90, "message": "완료"}


def _steps(*names):
    return {"cutover": {"steps": [{"step": n} for n in names]}}


class RecordingCtx:
    def __init__(self):
        self.reports = []

    def report(self, state, progress, metrics=None, message=None):
        self.reports.append(SimpleNamespace(state=state, progress=progress, metrics=metrics or {}, message=message))


def _drive_cutover(engine, monkeypatch, *jobs):
    """에이전트가 jobs 를 차례로 돌려줄 때 run_cutover 를 돌립니다. (보고 목록, 반환값 또는 예외)"""
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    remaining = list(jobs)

    class FakeAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)
    ctx = RecordingCtx()
    try:
        result = run_cutover(engine.create_run(REQUEST), ctx=ctx, poll_interval_s=0.001)  # type: ignore[arg-type]
    except Exception as exc:
        result = exc
    return ctx.reports, result


def test_cutover_seconds_prefer_worker_timestamps(engine, monkeypatch):
    evidence = {"cutover": {"t0": "2026-11-05T01:00:00.000Z", "t1": "2026-11-05T01:00:02.3456Z"}}
    reports, _ = _drive_cutover(
        engine, monkeypatch, {**STABILIZING, "metrics": {"cutover_ms": 9999.0}, "evidence": evidence}
    )
    m = reports[-1].metrics
    assert m["cutover_seconds"] == 2.346
    assert m["cutover_seconds_source"] == "worker"


def test_cutover_seconds_handle_mixed_offsets(engine, monkeypatch):
    evidence = {"cutover": {"t0": "2026-11-05T10:00:00+09:00", "t1": "2026-11-05T01:00:01+00:00"}}
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})
    assert reports[-1].metrics["cutover_seconds"] == 1.0


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "not-a-dict",
        {"cutover": "not-a-dict"},
        {"cutover": {}},
        {"cutover": {"t0": "2026-11-05T01:00:00Z"}},
        {"cutover": {"t0": 1, "t1": 2}},
        {"cutover": {"t0": True, "t1": False}},
        {"cutover": {"t0": "2026-11-05T01:00:00", "t1": "2026-11-05T01:00:01"}},
        {"cutover": {"t0": "2026-11-05T01:00:00Z", "t1": "2026-11-05T01:00:01"}},
        {"cutover": {"t0": "어제", "t1": "오늘"}},
        {"cutover": {"t0": "2026-11-05T01:00:05Z", "t1": "2026-11-05T01:00:01Z"}},
    ],
)
def test_cutover_seconds_fall_back_to_agent_report(engine, monkeypatch, evidence):
    reports, result = _drive_cutover(
        engine, monkeypatch, {**STABILIZING, "metrics": {"cutover_ms": 1250.0}, "evidence": evidence}
    )
    assert not isinstance(result, Exception)
    m = reports[-1].metrics
    assert m["cutover_seconds"] == 1.25
    assert m["cutover_seconds_source"] == "agent"


@pytest.mark.parametrize("metrics", [{}, None, {"cutover_ms": True}, {"cutover_ms": "1250"}, ["bad"]])
def test_cutover_seconds_are_absent_without_any_measurement(engine, monkeypatch, metrics):
    reports, result = _drive_cutover(engine, monkeypatch, {**STABILIZING, "metrics": metrics})
    assert not isinstance(result, Exception)
    assert not {"cutover_seconds", "cutover_seconds_source"} & set(reports[-1].metrics)


def test_sequence_verified_allows_interleaved_steps_and_keeps_only_listed_keys(engine, monkeypatch):
    steps = [
        {"step": "hold", "at": "2026-11-05T01:00:00Z", "elapsed_ms": 1.5, "secret": "x"},
        {"step": "inflight_zero", "held_count": 3, "elapsed_ms": 2},
        {"step": "source_writes_blocked", "sessions_ended": 1},
        {"step": "marker_on_target", "waited_ms": 12.5},
        {"step": "sink_paused"},
        {"step": "validated", "mismatch": 0},
        {"step": "switched_to_target"},
        {"step": "released"},
    ]
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": {"cutover": {"steps": steps}}})
    m = reports[-1].metrics
    assert m["cutover_sequence"] == "VERIFIED"
    assert "cutover_sequence_missing" not in m
    assert m["cutover_steps"][0] == {"step": "hold", "elapsed_ms": 1.5}
    assert m["cutover_steps"][1] == {"step": "inflight_zero", "elapsed_ms": 2}
    assert m["cutover_steps"][3] == {"step": "marker_on_target", "waited_ms": 12.5}
    assert m["cutover_steps"][5] == {"step": "validated", "mismatch": 0}
    assert "경고" not in reports[-1].message


def test_incomplete_sequence_warns_but_does_not_fail_the_stage(engine, monkeypatch):
    evidence = _steps("hold", "validated", "released")
    reports, result = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})

    assert result["phase"] == "stabilizing"
    last = reports[-1]
    assert last.metrics["cutover_sequence"] == "INCOMPLETE"
    assert last.metrics["cutover_sequence_missing"] == ["marker_on_target", "switched_to_target"]
    assert "트래픽 전환 완료" in last.message
    assert "경고" in last.message
    assert "marker_on_target" in last.message


def test_out_of_order_step_reports_only_the_displaced_name(engine, monkeypatch):
    evidence = _steps("hold", "validated", "marker_on_target", "switched_to_target", "released")
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})
    m = reports[-1].metrics
    assert m["cutover_sequence"] == "INCOMPLETE"
    assert m["cutover_sequence_missing"] == ["validated"]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "not-a-dict",
        {},
        {"cutover": None},
        {"cutover": {}},
        {"cutover": {"steps": "hold"}},
        {"cutover": {"steps": None}},
        {"cutover": {"steps": ["hold", "released"]}},
        {"cutover": {"steps": [{"step": "hold"}, "released"]}},
    ],
)
def test_missing_or_malformed_evidence_writes_no_step_metrics(engine, monkeypatch, evidence):
    reports, result = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})
    assert not isinstance(result, Exception)
    assert not STEP_KEYS & set(reports[-1].metrics)
    assert "경고" not in reports[-1].message


def test_simulator_writes_no_step_metrics(engine, monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.setenv("ALPHA_CDC_SIMULATION", "1")
    ctx = RecordingCtx()
    run_cutover(engine.create_run(REQUEST), ctx=ctx, poll_interval_s=0.001)  # type: ignore[arg-type]
    m = ctx.reports[-1].metrics
    assert not STEP_KEYS & set(m)
    assert m["cutover_seconds_source"] == "agent"


def test_step_values_of_the_wrong_type_are_dropped(engine, monkeypatch):
    steps = [
        {"step": "hold", "elapsed_ms": True, "waited_ms": "12", "mismatch": None, "error": 500},
        {"step": "marker_on_target", "elapsed_ms": float("nan")},
    ]
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": {"cutover": {"steps": steps}}})
    assert reports[-1].metrics["cutover_steps"] == [{"step": "hold"}, {"step": "marker_on_target"}]


def test_step_error_is_truncated(engine, monkeypatch):
    evidence = {"cutover": {"steps": [{"step": "failed", "error": "x" * 500}]}}
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})
    assert reports[-1].metrics["cutover_steps"][0]["error"] == "x" * 300


AGENT_ERROR = 'connection to server at "10.31.0.5", port 5432 failed: FATAL:  password authentication failed for user "alpha_admin"'
AGENT_DSN = "postgresql://alpha_admin:hunter2@db.internal.example:5432/app?sslmode=require"


def _assert_no_connection_details(text):
    for secret in ("10.31.0.5", "5432", "alpha_admin", "hunter2", "db.internal"):
        assert secret not in text


@pytest.mark.parametrize("error", [AGENT_ERROR, AGENT_ERROR.replace("FATAL:  ", "FATAL: ")])
def test_scrub_keeps_the_meaning_of_a_real_agent_error(error):
    scrubbed = _scrub_agent_text(error)
    _assert_no_connection_details(scrubbed)
    assert "password authentication failed" in scrubbed
    assert "connection to server" in scrubbed
    assert "FATAL" in scrubbed


def test_scrub_removes_a_dsn():
    scrubbed = _scrub_agent_text(f"could not connect to {AGENT_DSN}: timeout expired")
    _assert_no_connection_details(scrubbed)
    assert "could not connect to" in scrubbed
    assert "timeout expired" in scrubbed


@pytest.mark.parametrize(
    ("text", "secrets", "kept"),
    [
        ("connect https://gate.example.com/control/target?x=1 failed", ["gate.example"], "connect"),
        ("host=10.31.0.5 port=5432 dbname=app user=alpha_admin password=hunter2 failed", ["10.31.0.5", "5432", "alpha_admin", "hunter2"], "failed"),
        ("hostname=db.internal.example port=5432 refused", ["db.internal", "5432"], "refused"),
        ("host='db.internal' user='alpha admin' password='hun ter2' refused", ["db.internal", "alpha", "admin", "hun", "ter2"], "refused"),
        ("PASSWORD=hunter2 refused", ["hunter2"], "refused"),
        ("password: hunter2 refused", ["hunter2"], "refused"),
        ('role "alpha_admin" does not exist', ["alpha_admin"], "does not exist"),
        ("Access denied for user 'alpha_admin'@'10.31.0.5'", ["alpha_admin", "10.31.0.5"], "Access denied"),
        ('connection to server at "app.abc.ap-northeast-2.rds.amazonaws.com" (10.31.0.5), port 5432 failed', ["rds.amazonaws", "10.31.0.5", "5432"], "failed"),
        ("timeout talking to app.abc.ap-northeast-2.rds.amazonaws.com now", ["rds.amazonaws"], "now"),
        (r"password='hun\'ter2 x' refused", ["hun", "ter2"], "refused"),
        ("password=hun,ter2;x refused", ["hun", "ter2"], "refused"),
        ("password='cut off hunter2", ["hunter2", "cut off"], "password"),
        ("timeout talking to 10.31.0.5:5432", ["10.31.0.5", "5432"], "timeout talking to"),
        ("timeout talking to db.internal.example:5432", ["db.internal", "5432"], "timeout talking to"),
        ("timeout talking to localhost:5432", ["localhost", "5432"], "timeout talking to"),
        ('could not translate host name "db.internal" to address', ["db.internal"], "could not translate"),
        ('Is the server running on host "db.internal" and accepting TCP/IP connections on port 5432?', ["db.internal", "5432"], "accepting"),
        ('connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed', ["/var/run", "5432"], "failed"),
        ("timeout talking to 2001:db8::1", ["2001:db8::1"], "timeout talking to"),
        ("timeout talking to [2001:db8::1]:5432 now", ["2001:db8", "5432"], "now"),
        ("timeout talking to fe80::1%eth0: refused", ["fe80::1", "eth0"], "refused"),
        ("mapped ::ffff:10.31.0.5 refused", ["10.31.0.5"], "refused"),
        ("bad sslpassword=hunter2 and everything after it", ["hunter2", "everything after"], "bad"),
        ("bad SSLPASSWORD hunter2 everything after", ["hunter2", "everything after"], "bad"),
        ("bad token=abc123 password=x host=y", ["abc123", "host=y"], "bad"),
        ("bad access_token=abc123\nsecond line", ["abc123", "second line"], "bad"),
    ],
)
def test_scrub_removes_connection_details(text, secrets, kept):
    scrubbed = _scrub_agent_text(text)
    for secret in secrets:
        assert secret not in scrubbed
    assert kept in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "전환 실패, 원본으로 재개: marker timeout",
        "marker wait exceeded at 12:30:45 after 3 tries",
        "invalid token in response",
        "password authentication failed",
        "user does not exist",
        "cutover_ready: 2 tables mismatch",
        "psycopg2.errors.OperationalError: server closed the connection",
    ],
)
def test_scrub_leaves_ordinary_text_alone(text):
    assert _scrub_agent_text(text) == text


@pytest.mark.parametrize("value", [None, 5, 1.5, True, b"bytes", ["x"], {"a": 1}, object()])
def test_scrub_returns_none_for_non_strings(value):
    assert _scrub_agent_text(value) is None


def test_scrub_truncates_after_masking():
    scrubbed = _scrub_agent_text("host=10.31.0.5 " + "x" * 500)
    assert len(scrubbed) == 300
    assert "10.31.0.5" not in scrubbed


def test_scrub_handles_a_huge_single_token_quickly():
    assert len(_scrub_agent_text("x" * 2_000_000)) == 300


def test_step_error_is_scrubbed(engine, monkeypatch):
    evidence = {"cutover": {"steps": [{"step": "failed", "error": AGENT_ERROR}]}}
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": evidence})
    error = reports[-1].metrics["cutover_steps"][0]["error"]
    _assert_no_connection_details(error)
    assert "password authentication failed" in error


@pytest.mark.parametrize(
    "job",
    [
        {"state": "waiting_approval", "phase": "cutover_ready"},
        {"state": "failed", "phase": "cutover"},
        {"state": "aborted", "phase": "cutover"},
    ],
    ids=["rollback", "failed", "aborted"],
)
@pytest.mark.parametrize("text", [AGENT_ERROR, f"{AGENT_DSN} password authentication failed"], ids=["error", "dsn"])
def test_agent_message_is_scrubbed_in_failure_exceptions(engine, monkeypatch, job, text):
    _, result = _drive_cutover(engine, monkeypatch, {**job, "message": text})

    assert isinstance(result, RuntimeError)
    _assert_no_connection_details(str(result))
    assert "password authentication failed" in str(result)


def test_agent_message_is_scrubbed_when_the_request_was_refused(engine, monkeypatch):
    from app.features.db_migration.client import AgentError

    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class RefusingAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            raise AgentError(409, "not ready")

        def get_job(self, job_id):
            return {"state": "running", "phase": "streaming", "message": AGENT_ERROR}

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", RefusingAgent)

    with pytest.raises(RuntimeError) as caught:
        run_cutover(engine.create_run(REQUEST), ctx=RecordingCtx(), poll_interval_s=0.001)  # type: ignore[arg-type]

    _assert_no_connection_details(str(caught.value))
    assert "password authentication failed" in str(caught.value)


@pytest.mark.parametrize("message", [5, {"host": "10.31.0.5"}, ["10.31.0.5"]])
def test_non_string_agent_message_falls_back_to_unknown_detail(engine, monkeypatch, message):
    _, result = _drive_cutover(engine, monkeypatch, {"state": "failed", "phase": "cutover", "message": message})
    assert str(result) == "컷오버 실패 (failed): 상세 불명"


def test_agent_message_is_scrubbed_in_progress_and_success_reports(engine, monkeypatch):
    reports, _ = _drive_cutover(
        engine,
        monkeypatch,
        {"state": "running", "phase": "cutover", "message": AGENT_ERROR},
        {**STABILIZING, "message": AGENT_ERROR},
    )
    for report in reports:
        _assert_no_connection_details(report.message or "")
    assert "password authentication failed" in reports[-1].message


def test_cutover_path_does_not_forward_measure_error(engine, monkeypatch):
    metrics = {"mismatch": 0, "measure_error": AGENT_ERROR}
    reports, _ = _drive_cutover(
        engine,
        monkeypatch,
        {"state": "running", "phase": "cutover", "metrics": metrics},
        {**STABILIZING, "metrics": metrics},
    )
    assert all("measure_error" not in report.metrics for report in reports)
    assert reports[-1].metrics["mismatch"] == 0


def test_db_migration_path_does_not_forward_measure_error(engine, monkeypatch):
    import threading

    stop_event = threading.Event()

    def fake_run(inp, proxy_ctx):
        proxy_ctx.report("running", 40, {"phase": "copying", "measure_error": AGENT_ERROR}, None)
        proxy_ctx.report("running", 50, {"phase": "streaming", "copy_percent": 100}, None)
        stop_event.wait(timeout=2.0)
        return {"state": "stopped", "job_id": inp["run_id"]}

    monkeypatch.setattr("app.features.db_migration.service.run", fake_run, raising=False)
    ctx = RecordingCtx()
    run = engine.create_run(REQUEST).model_copy(
        update={
            "outputs": {
                "planning": {"source": {"tables": ["public.users"]}},
                "provisioning": {"target": {"project_id": "p-1"}},
            }
        }
    )

    out = run_db_migration(run, ctx=ctx, poll_interval_s=0.001)  # type: ignore[arg-type]
    stop_event.set()

    assert all("measure_error" not in report.metrics for report in ctx.reports)
    assert any(report.metrics.get("phase") == "copying" for report in ctx.reports)
    assert "measure_error" not in out["metrics"]
    assert out["metrics"]["copy_percent"] == 100


def test_items_without_a_step_name_are_dropped_and_cannot_verify(engine, monkeypatch):
    steps = [{"step": n} for n in SEQUENCE[:-1]] + [{"step": 5}, {"elapsed_ms": 1}]
    reports, _ = _drive_cutover(engine, monkeypatch, {**STABILIZING, "evidence": {"cutover": {"steps": steps}}})
    m = reports[-1].metrics
    assert len(m["cutover_steps"]) == 4
    assert m["cutover_sequence"] == "INCOMPLETE"
    assert m["cutover_sequence_missing"] == ["released"]


def test_empty_steps_count_as_supplied_and_never_verify(engine, monkeypatch):
    # 에이전트는 시도마다 steps: [] 로 시작합니다. 진행 중에는 이전 시도 증적을 비우고, 성공인데 비어 있으면 경고합니다.
    empty = {"cutover": {"steps": []}}
    reports, _ = _drive_cutover(
        engine,
        monkeypatch,
        {"state": "running", "phase": "cutover", "progress": 75, "evidence": empty},
        {**STABILIZING, "evidence": empty},
    )
    assert reports[-2].metrics["cutover_steps"] == []
    assert "cutover_sequence" not in reports[-2].metrics
    assert reports[-1].metrics["cutover_sequence"] == "INCOMPLETE"
    assert reports[-1].metrics["cutover_sequence_missing"] == list(SEQUENCE)


def test_polling_reports_steps_live_without_a_sequence_verdict(engine, monkeypatch):
    reports, _ = _drive_cutover(
        engine,
        monkeypatch,
        {"state": "running", "phase": "cutover", "progress": 80, "metrics": {"mismatch": 0}, "evidence": _steps("hold")},
        {**STABILIZING, "evidence": _steps(*SEQUENCE)},
    )
    live = next(r for r in reports if r.progress == 80)
    assert live.metrics["cutover_steps"] == [{"step": "hold"}]
    assert live.metrics["mismatch"] == 0
    assert "cutover_sequence" not in live.metrics
    assert reports[-1].metrics["cutover_sequence"] == "VERIFIED"


ROLLBACK_EVIDENCE = {"cutover": {"steps": [{"step": "hold"}, {"step": "failed", "error": "marker timeout"}]}}


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"state": "waiting_approval", "phase": "cutover_ready", "progress": 70, "message": "원본 재개"}, "컷오버 실패로 원본으로 롤백되었습니다: 원본 재개"),
        ({"state": "failed", "phase": "cutover", "progress": 75, "message": "수동 확인"}, "컷오버 실패 (failed): 수동 확인"),
        ({"state": "aborted", "phase": "cutover", "progress": 75}, "컷오버 실패 (aborted): 상세 불명"),
    ],
)
def test_steps_are_reported_before_raising_on_failure(engine, monkeypatch, job, expected):
    reports, result = _drive_cutover(engine, monkeypatch, {**job, "evidence": ROLLBACK_EVIDENCE})

    assert isinstance(result, RuntimeError)
    assert str(result) == expected
    last = reports[-1]
    assert last.state == "running"
    assert last.metrics["cutover_steps"][-1] == {"step": "failed", "error": "marker timeout"}
    assert "cutover_sequence" not in last.metrics


@pytest.mark.parametrize("progress", [None, 150, -3, "70", True])
def test_bad_agent_progress_does_not_hide_the_rollback_message(engine, monkeypatch, progress):
    from app.features.workflows.context import StoreRunContext
    from app.features.workflows.schemas import WorkflowState as State

    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class FakeAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return {"state": "waiting_approval", "phase": "cutover_ready", "progress": progress, "message": "롤백"}

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)
    run = engine.create_run(REQUEST)
    engine._store.update(run.run_id, lambda r: r.model_copy(update={"state": State.RUNNING}))
    ctx = StoreRunContext(engine._store, run.run_id, StageName.CUTOVER)

    with pytest.raises(RuntimeError, match="컷오버 실패로 원본으로 롤백되었습니다: 롤백"):
        run_cutover(run, ctx=ctx, poll_interval_s=0.001)


def test_bad_agent_progress_while_polling_does_not_raise(engine, monkeypatch):
    reports, result = _drive_cutover(
        engine,
        monkeypatch,
        {"state": "running", "phase": "cutover", "progress": None},
        {**STABILIZING},
    )
    assert not isinstance(result, Exception)
    assert all(0 <= r.progress <= 100 for r in reports)


def test_abort_during_failure_report_still_propagates(engine, monkeypatch):
    from app.features.workflows.context import RunAborted

    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")

    class FakeAgent:
        def __init__(self, settings):
            pass

        def cutover(self, job_id, approved_by):
            return {"accepted": True}

        def get_job(self, job_id):
            return {"state": "waiting_approval", "phase": "cutover_ready", "evidence": ROLLBACK_EVIDENCE}

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    class AbortingCtx:
        def __init__(self):
            self.calls = 0

        def report(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise RunAborted("mig")

    with pytest.raises(RunAborted):
        run_cutover(engine.create_run(REQUEST), ctx=AbortingCtx(), poll_interval_s=0.001)  # type: ignore[arg-type]
