from fastapi.testclient import TestClient

from app.features.assessment import db as db_part
from app.features.assessment import service
from app.features.assessment.schemas import AssessmentInput, DbAssessment, DbInfo, SourceDb, TableInfo
from app.main import app

from .fakes import ACCOUNT, session

INP = AssessmentInput(aws_credentials_ref="secret://run-1/source-cloud", db_admin_ref="secret://run-1/source-db-admin",
                      aws_region="ap-northeast-2", target_region="asia-northeast3")


def test_db_not_implemented_blocks_approval_but_app_part_flows():
    calls = []

    class Ctx:
        def report(self, state, progress, metrics=None, message=None):
            calls.append((state, progress))

    out = service.run(INP, Ctx(), session=session())
    assert any("DB·네트워크 조회" in g for g in out.gaps)  # 가짜 세션엔 rds 응답이 없어 조회 실패 gap
    assert not out.plan.approvable  # DB 값이 없으니 unknown → 승인 불가 (가짜 통과 없음)
    checks = {c.id: c.result for c in out.plan.checks}
    assert checks["image_available"] == "pass"
    assert checks["pg_version_compatible"] == "unknown"
    # 앱 쪽 값은 target_infra 로, PGUSER 는 db_user 로
    assert out.plan.target_infra.db_user == "alpha"
    assert out.plan.target_infra.container_port == 8080
    # source: cdc 명세 칸
    assert out.source.account_id == ACCOUNT and out.source.region == "ap-northeast-2"
    assert out.source.app.db_user == "alpha"
    assert out.source.credentials_ref.cloud == "secret://run-1/source-cloud"
    assert out.source.credentials_ref.db_admin == "secret://run-1/source-db-admin"
    assert calls[-1] == ("running", 100)
    assert all(state == "running" for state, _ in calls)


def test_db_part_filled_makes_plan_approvable(monkeypatch):
    def fake_db(inp, sess):
        return DbAssessment(
            facts=DbInfo(engine="postgres", engine_version="16.15", database_name="postgres",
                         instance_class="db.t3.micro", allocated_storage_gb=20, logical_replication=True,
                         publicly_accessible=True,
                         tables=[TableInfo(name="work_items", has_primary_key=True)]),
            source_db=SourceDb(instance_id="db-alpha-target", port=5432, version="16.15"),
        )

    monkeypatch.setattr(db_part, "collect", fake_db)
    out = service.run(INP, session=session())
    assert out.plan.approvable, out.plan.blocking
    assert out.source.tables == ["public.work_items"]
    assert out.source.db.instance_id == "db-alpha-target"
    assert any(r.level == "high" and "퍼블릭" in r.title for r in out.plan.risks)


def test_db_part_crash_is_gap_not_crash(monkeypatch):
    def boom(inp, sess):
        raise RuntimeError("bug")

    monkeypatch.setattr(db_part, "collect", boom)
    out = service.run(INP, session=session())
    assert any("RuntimeError" in g for g in out.gaps)
    assert not out.plan.approvable


def test_unusable_credentials_ref_gives_gap_and_no_approval():
    out = service.run(INP.model_copy(update={"aws_credentials_ref": "plain-key"}))
    assert out.gaps and not out.plan.approvable
    assert out.source.account_id is None


def test_run_router():
    body = INP.model_dump() | {"aws_credentials_ref": "plain-key"}
    res = TestClient(app).post("/assessment/run", json=body)
    assert res.status_code == 200
    assert res.json()["plan"]["approvable"] is False


def test_db_and_app_lookups_run_at_the_same_time(monkeypatch):
    import threading

    from app.features.assessment import app as app_part
    from app.features.assessment.schemas import AppAssessment, AppInfo

    app_started = threading.Event()
    seen = {}

    def fake_app(inp, sess):
        app_started.set()
        return AppAssessment(facts=AppInfo(service_name="target-app"))

    def fake_db(inp, sess):
        seen["app_ran_meanwhile"] = app_started.wait(timeout=2)  # 차례로 돌면 2초 기다리다 False
        return DbAssessment(gaps=["db gap"])

    monkeypatch.setattr(app_part, "collect", fake_app)
    monkeypatch.setattr(db_part, "collect", fake_db)
    out = service.run(INP, session=session())
    assert seen["app_ran_meanwhile"] is True
    assert "db gap" in out.gaps
