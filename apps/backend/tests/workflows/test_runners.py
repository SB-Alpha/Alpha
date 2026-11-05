import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.features.workflows.engine import Engine
from app.features.workflows.runners import (
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

