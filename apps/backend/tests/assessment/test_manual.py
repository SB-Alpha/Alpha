import pytest
from pydantic import ValidationError

from app.features.assessment import service
from app.features.assessment.schemas import AssessmentInput

MANUAL = {
    "cloud": "azure",
    "region": "koreacentral",
    "app": {
        "service_name": "Target_App",
        "image_uri": "registry.example.com/target-app:1.0",
        "container_port": 8080,
        "vcpu": 0.5,
        "memory_mib": 1024,
        "health_check_path": "/health/ready",
        "env_vars": {"LOG_LEVEL": "INFO", "DB_PASSWORD": "x", "DATABASE_URL": "postgres://u:p@h/db"},
        "db_user": "alpha",
    },
    "db": {
        "engine_version": "16.4",
        "host": "10.0.0.5",
        "port": 5432,
        "database_name": "workitems",
        "memory_gib": 1,
        "vcpu": 2,
        "storage_gb": 20,
        "publicly_accessible": False,
        "logical_replication": True,
        "tables": [{"name": "work_items", "has_primary_key": True}],
    },
}


def _inp(**manual_overrides):
    manual = {**MANUAL, **manual_overrides}
    return AssessmentInput(source_kind="manual", manual=manual, target_region="asia-northeast3",
                           db_admin_ref="secret://run-1/source-db-admin")


def test_manual_source_gives_same_output_shape_without_aws():
    out = service.run(_inp())
    infra = out.plan.target_infra
    assert out.plan.approvable  # 입력한 값으로 4개 조건 판정
    assert infra.cpu == 1 and infra.memory == 1024 and infra.container_port == 8080
    assert infra.source_service_name == "target-app"  # 이미지 저장소 이름 우선 (AWS 와 같은 규칙)
    assert (infra.db_memory_gib, infra.db_vcpu, infra.db_tier) == (1, 2, "db-g1-small")
    assert infra.db_user == "alpha" and infra.db_disk_size_gb == 20
    assert out.source.region == "koreacentral" and out.source.account_id is None
    assert out.source.db.host == "10.0.0.5" and out.source.tables == ["public.work_items"]
    assert out.source.credentials_ref.cloud is None
    assert out.source.credentials_ref.db_admin == "secret://run-1/source-db-admin"


def test_manual_values_are_flagged_as_unverified():
    out = service.run(_inp())
    assert any("직접 입력" in g and "azure" in g for g in out.gaps)
    assert any("직접 입력" in r.detail or "직접 입력" in r.title for r in out.plan.risks)


def test_manual_env_secrets_are_dropped():
    out = service.run(_inp())
    assert out.plan.target_infra.env_vars == {"LOG_LEVEL": "INFO"}
    dropped = [g for g in out.gaps if "비밀로 보여" in g]
    assert len(dropped) == 2 and not any("u:p@" in g for g in out.gaps)


def test_unknown_manual_values_are_not_invented():
    out = service.run(_inp(app={"service_name": "svc"}, db={}))
    checks = {c.id: c.result for c in out.plan.checks}
    assert checks["pg_version_compatible"] == "unknown" and checks["image_available"] != "pass"
    assert not out.plan.approvable
    infra = out.plan.target_infra
    assert infra.db_memory_gib is None and infra.db_vcpu is None and infra.db_tier is None
    assert any("이미지 주소 미입력" in g for g in out.gaps)


def test_source_kind_requires_its_values():
    with pytest.raises(ValidationError):
        AssessmentInput(target_region="asia-northeast3")  # aws 기본값인데 키·리전 없음
    with pytest.raises(ValidationError):
        AssessmentInput(source_kind="manual", target_region="asia-northeast3")
    # 예전 형식(aws)은 그대로 받음
    AssessmentInput(aws_credentials_ref="secret://r/source-cloud", aws_region="ap-northeast-2",
                    target_region="asia-northeast3")
