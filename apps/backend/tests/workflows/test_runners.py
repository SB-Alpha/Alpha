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
        "planning": {"source": {"account_id": "123"}},
        "provisioning": {"target": {"project_id": "p-1"}},
    }})

    out = run_db_migration(run, ctx=None)  # type: ignore[arg-type]
    assert out == {"state": "streaming", "job_id": "job-1"}
    assert calls[0]["run_id"] == run.run_id
    assert calls[0]["source"] == {"account_id": "123"}
    assert calls[0]["target"] == {"project_id": "p-1"}


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


def test_cutover_runner_calls_agent(engine, monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://fake-agent")
    calls = []

    class FakeAgent:
        def __init__(self, settings):
            self.settings = settings

        def cutover(self, job_id, approved_by):
            calls.append((job_id, approved_by))
            return {"switched": True}

    monkeypatch.setattr("app.features.db_migration.client.CdcAgentClient", FakeAgent)

    run = engine.create_run(REQUEST)
    reports = []

    class FakeCtx:
        def report(self, state, progress, metrics=None, message=None):
            reports.append((state, progress, message))

    out = run_cutover(run, ctx=FakeCtx())  # type: ignore[arg-type]
    assert out == {"switched": True}
    assert calls == [(run.run_id.replace("-", "_"), "workflows")]
    assert reports[-1][1] == 100
