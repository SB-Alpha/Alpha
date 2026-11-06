import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.features.assessment.schemas import PlanningInput
from app.features.assessment.plan import build as run
from app.features.assessment.rules import gcp_name
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


def _ready_input(**db_overrides) -> PlanningInput:
    db = {
        "engine": "postgres",
        "engine_version": "16.4",
        "database_name": "workitems",
        "tables": [{"name": "work_items", "row_count": 1000, "has_primary_key": True}],
        "logical_replication": True,
        "username": "alpha",
        "instance_class": "db.t3.micro",
        "allocated_storage_gb": 20,
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
                "env_vars": {"LOG_LEVEL": "INFO", "APP_ENV": "aws", "PGHOST": "x"},
                "secret_refs": {
                    "PGPASSWORD": "arn:aws:secretsmanager:ap-northeast-2:123:secret:pg",
                    "API_KEY": "arn:aws:secretsmanager:ap-northeast-2:123:secret:api",
                },
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
    infra = plan.target_infra
    assert infra.cpu == 1 and infra.memory == 1024
    assert infra.db_version == "16"
    assert infra.db_tier == "db-g1-small" and infra.db_disk_size_gb == 20 and infra.db_user == "alpha"
    # 클라우드 중립 값: 다른 목적지가 자기 등급·이름 규칙으로 바꿔 씀
    assert infra.db_memory_gib == 1 and infra.db_vcpu == 2  # db.t3.micro
    # provisioning 이 채우는 PG*·APP_ENV 는 빠지고, 나머지만 넘어감
    assert infra.env_vars == {"LOG_LEVEL": "INFO"}
    assert infra.secret_refs == {"API_KEY": "target-app-api-key"}
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
    # 평평한 구조: 값은 스칼라 또는 문자열 map 만
    for v in tfvars.values():
        assert not isinstance(v, (list, dict)) or all(isinstance(x, str) for x in v.values())


def test_unknown_db_size_is_left_to_provisioning_default():
    plan = run(_ready_input(instance_class="db.r5.large", allocated_storage_gb=None, username=None))
    tfvars = plan.target_infra.to_tfvars()
    assert "db_tier" not in tfvars and "db_disk_size_gb" not in tfvars and "db_user" not in tfvars
    assert "db_memory_gib" not in tfvars and "db_vcpu" not in tfvars  # 모르는 크기는 지어내지 않음
    assert any("DB 등급" in r.title for r in plan.risks)


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
    res = client.post("/assessment/plan", json=body)
    assert res.status_code == 200
    assert res.json()["approvable"] is False


def test_secret_values_must_move_before_deploy():
    plan = run(_ready_input())
    risk = next(r for r in plan.risks if r.title == "비밀값 이전 필요")
    # PGPASSWORD 는 provisioning 이 만들므로 빠지고, API_KEY 만 옮길 대상
    assert risk.level == "high" and "API_KEY" in risk.detail and "PGPASSWORD" not in risk.detail


def test_gcp_name_rule():
    from app.features.assessment.rules import gcp_name

    assert gcp_name("Target_App") == "target-app"
    assert gcp_name("1st-service").startswith("app-")
    assert len(gcp_name("x" * 80)) <= 49


def test_source_image_pinned_to_digest_when_known():
    from app.features.assessment.rules import pinned_image

    repo = "123.dkr.ecr.ap-northeast-2.amazonaws.com/target-app"
    assert pinned_image(f"{repo}:abc", "sha256:ff") == f"{repo}@sha256:ff"
    assert pinned_image(f"{repo}@sha256:ff", "sha256:ff") == f"{repo}@sha256:ff"
    assert pinned_image(f"{repo}:abc", None) == f"{repo}:abc"
    assert pinned_image("localhost:5000/app:1", "sha256:ff") == "localhost:5000/app@sha256:ff"
    assert pinned_image(None, "sha256:ff") is None


def test_service_name_comes_from_image_repo_not_ecs_service():
    inp = _ready_input()
    inp.inventory.app.service_name = "target-app-task-service-gtzbb08o"
    plan = run(inp)
    assert plan.target_infra.service_name == "target-app"
    assert plan.target_infra.secret_refs == {"API_KEY": "target-app-api-key"}


def test_service_name_falls_back_to_ecs_name_without_image():
    inp = _ready_input()
    inp.inventory.app.service_name = "My_Service"
    inp.inventory.app.image_uri = None
    plan = run(inp)
    assert plan.target_infra.service_name == "my-service"


def test_source_service_name_keeps_name_before_gcp_rules():
    plan = run(_ready_input())
    infra = plan.target_infra
    assert infra.service_name == gcp_name(infra.source_service_name)


def test_cloud_neutral_fields_are_not_gcp_terraform_variables():
    from app.features.provisioning.service import build_tfvars

    tfvars = build_tfvars(run(_ready_input()).target_infra.to_tfvars(), project_id="p", deploy_app=False)
    assert not {"source_service_name", "db_memory_gib", "db_vcpu"} & set(tfvars)
