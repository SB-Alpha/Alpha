import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.features.planning.schemas import PlanningInput
from app.features.planning.service import run
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


def _ready_input(**db_overrides) -> PlanningInput:
    db = {
        "engine": "postgres",
        "engine_version": "16.4",
        "database_name": "workitems",
        "tables": [{"name": "work_items", "row_count": 1000, "has_primary_key": True}],
        "logical_replication": True,
    }
    db.update(db_overrides)
    return PlanningInput.model_validate({
        "inventory": {
            "app": {
                "service_name": "target-app",
                "image_uri": "123.dkr.ecr.ap-northeast-2.amazonaws.com/target-app:abc",
                "container_port": 8080,
                "cpu_units": 512,
                "memory_mib": 1024,
                "health_check_path": "/health/ready",
                "secret_refs": ["arn:aws:secretsmanager:ap-northeast-2:123:secret:pg"],
            },
            "db": db,
        },
        "target": {"region": "asia-northeast3"},
    })


def _check(plan, check_id):
    return next(c for c in plan.checks if c.id == check_id)


def test_all_checks_pass_is_approvable():
    plan = run(_ready_input())
    assert [c.result for c in plan.checks] == ["pass"] * 4
    assert plan.approvable and plan.blocking == []
    assert plan.target_infra.cpu == "1"
    assert plan.target_infra.memory == "1024Mi"
    assert plan.target_infra.db_version == "POSTGRES_16"
    assert len(plan.summary) == 3
    assert [s.needs_approval for s in plan.steps].count(True) == 2


def test_missing_pk_fails():
    plan = run(_ready_input(tables=[{"name": "logs", "has_primary_key": False}]))
    assert _check(plan, "primary_keys_present").result == "fail"
    assert not plan.approvable


def test_logical_replication_off_fails():
    plan = run(_ready_input(logical_replication=False, wal_level="replica"))
    assert _check(plan, "logical_replication_enabled").result == "fail"
    assert not plan.approvable


def test_pg_downgrade_and_too_old_fail():
    assert _check(run(_ready_input(engine_version="17.1")), "pg_version_compatible").result == "fail"
    assert _check(run(_ready_input(engine_version="9.6")), "pg_version_compatible").result == "fail"


def test_unknown_values_block_approval_not_pass():
    plan = run(_ready_input(logical_replication=None, wal_level=None))
    assert _check(plan, "logical_replication_enabled").result == "unknown"
    assert not plan.approvable


def test_target_infra_converts_to_tfvars():
    plan = run(_ready_input())
    tfvars = plan.target_infra.to_tfvars()
    json.dumps(tfvars)  # tfvars.json 으로 그대로 쓸 수 있어야 함
    assert tfvars["region"] == "asia-northeast3"
    assert tfvars["db_logical_decoding"] is True


def test_reports_progress_to_ctx():
    calls = []

    class Ctx:
        def report(self, state, progress, metrics=None, message=None):
            calls.append((state, progress))

    run(_ready_input(), Ctx())
    assert calls[-1] == ("waiting_approval", 100)


def test_sample_fixture_matches_committed_plan():
    inp = PlanningInput.model_validate(json.loads((FIXTURES / "inventory.sample.json").read_text(encoding="utf-8")))
    plan = run(inp).model_dump(mode="json", exclude={"generated_at"})
    expected = json.loads((FIXTURES / "plan.sample.json").read_text(encoding="utf-8"))
    expected.pop("generated_at", None)
    assert plan == expected
    assert plan["approvable"] is False  # 손 작성 입력은 값이 비어 있어 승인 불가가 맞음


def test_router():
    client = TestClient(app)
    body = json.loads((FIXTURES / "inventory.sample.json").read_text(encoding="utf-8"))
    res = client.post("/planning/plan", json=body)
    assert res.status_code == 200
    assert res.json()["approvable"] is False
