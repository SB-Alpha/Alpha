import pytest

from app.features.assessment import app as app_part
from app.features.assessment.schemas import AssessmentInput

from .fakes import ACCOUNT, DIGEST, IMAGE, FakeAwsError, container, session


def _inp(**kw) -> AssessmentInput:
    base = {"aws_credentials_ref": "secret://run-1/source-cloud", "aws_region": "ap-northeast-2",
            "target_region": "asia-northeast3", "github_repo_url": "https://github.com/SB-Alpha/target-app"}
    base.update(kw)
    return AssessmentInput(**base)


def test_reads_ecs_and_ecr():
    s = session()
    res = app_part.collect(_inp(), s)
    f = res.facts
    assert res.account_id == ACCOUNT
    assert f.service_name == "target-app"
    assert f.image_uri == IMAGE and f.image_digest == DIGEST
    assert f.container_port == 8080 and f.cpu_units == 512 and f.memory_mib == 1024
    assert f.health_check_path == "/health/ready"
    assert f.secret_refs == {"PGPASSWORD": f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:pg"}
    assert res.db_user == "alpha"
    # ECR 은 이미지 주소의 리전으로 조회
    assert s.regions["ecr"] == "ap-northeast-2"


def test_plain_env_that_looks_secret_is_dropped():
    res = app_part.collect(_inp(), session())
    assert "DB_PASSWORD" not in res.facts.env_vars
    assert res.facts.env_vars["LOG_LEVEL"] == "INFO"
    assert any("DB_PASSWORD" in g for g in res.gaps)
    assert "s3cr3t-plain" not in res.model_dump_json()


def test_health_path_from_target_group_when_container_has_none():
    svc = {"serviceName": "target-app", "taskDefinition": "arn:task/x:1",
           "loadBalancers": [{"targetGroupArn": "arn:tg/x", "containerName": "app", "containerPort": 8080}]}
    s = session(container(healthCheck=None), service=svc,
                elbv2={"describe_target_groups": {"TargetGroups": [{"HealthCheckPath": "/healthz"}]}})
    assert app_part.collect(_inp(), s).facts.health_check_path == "/healthz"


def test_many_clusters_needs_input_and_does_not_guess():
    s = session(ecs={"list_clusters": {"clusterArns": ["arn:cluster/a", "arn:cluster/b"]}})
    res = app_part.collect(_inp(), s)
    assert res.facts.image_uri is None
    assert res.facts.service_name == "target-app"  # repo 이름으로 대신하고 gap 에 남김
    assert any("ecs_cluster" in g for g in res.gaps)


def test_given_cluster_and_service_skip_listing():
    s = session(ecs={"list_clusters": FakeAwsError("AccessDeniedException")})
    res = app_part.collect(_inp(ecs_cluster="alpha", ecs_service="target-app"), s)
    assert res.facts.image_uri == IMAGE
    assert not any("클러스터" in g for g in res.gaps)


def test_access_denied_becomes_gap_with_code_only():
    s = session(ecs={"describe_task_definition": FakeAwsError("AccessDeniedException")})
    res = app_part.collect(_inp(), s)
    assert res.facts.image_uri is None
    assert any("AccessDeniedException" in g for g in res.gaps)
    assert "must not leak" not in res.model_dump_json()


def test_missing_pguser_is_reported_not_guessed():
    env = [{"name": "LOG_LEVEL", "value": "INFO"}]
    res = app_part.collect(_inp(), session(container(environment=env)))
    assert res.db_user is None
    assert any("PGUSER" in g for g in res.gaps)


def test_digest_in_image_is_used_without_ecr_call():
    s = session(container(image=f"{IMAGE.split(':abc')[0]}@{DIGEST}"))
    assert app_part.collect(_inp(), s).facts.image_digest == DIGEST
    assert s.clients["ecr"].calls == []


def _env_session(env: list[dict]):
    return session(container(environment=env))


def test_value_with_credentials_is_dropped_even_with_plain_name():
    leaked = "postgres://alpha:pl41n-pw@db.example:5432/app"
    res = app_part.collect(_inp(), _env_session([
        {"name": "DATABASE_URL", "value": leaked},
        {"name": "UPSTREAM", "value": "AKIAABCDEFGHIJKLMNOP"},
        {"name": "PUBLIC_URL", "value": "https://example.com/app"},
    ]))
    assert "DATABASE_URL" not in res.facts.env_vars and "UPSTREAM" not in res.facts.env_vars
    assert res.facts.env_vars["PUBLIC_URL"] == "https://example.com/app"
    assert any("DATABASE_URL" in g for g in res.gaps) and any("UPSTREAM" in g for g in res.gaps)
    dumped = res.model_dump_json()
    assert "pl41n-pw" not in dumped and "AKIAABCDEFGHIJKLMNOP" not in dumped


def test_names_ending_in_key_are_secret_but_plain_names_are_not():
    assert app_part.looks_secret("SIGNING_KEY") and app_part.looks_secret("AWS_ACCESS_KEY_ID")
    assert not app_part.looks_secret("KEYCLOAK_REALM") and not app_part.looks_secret("LOG_LEVEL")


# --- 조회 정확도 (Codex 검수 11/7) ---

def test_health_path_drops_query_and_fragment():
    c = container(healthCheck={"command": ["CMD-SHELL", "curl -f http://localhost:8080/health?token=qq-health-tok#x || exit 1"]})
    res = app_part.collect(_inp(), session(c))
    assert res.facts.health_check_path == "/health"
    assert "qq-health-tok" not in res.model_dump_json()


def test_load_balancer_port_wins_over_first_port_mapping():
    c = container(portMappings=[{"containerPort": 9090}, {"containerPort": 8080}])
    svc = {"serviceName": "target-app", "taskDefinition": "arn:task/x:1",
           "loadBalancers": [{"targetGroupArn": "arn:tg/x", "containerName": "app", "containerPort": 8080}]}
    s = session(c, service=svc, elbv2={"describe_target_groups": {"TargetGroups": [{"HealthCheckPath": "/h"}]}})
    assert app_part.collect(_inp(), s).facts.container_port == 8080


def test_many_ports_without_load_balancer_is_reported():
    c = container(portMappings=[{"containerPort": 9090}, {"containerPort": 8080}])
    res = app_part.collect(_inp(), session(c))
    assert res.facts.container_port == 9090
    assert any("포트가 2개" in g for g in res.gaps)


def test_running_task_digest_wins_over_tag_lookup():
    running = "sha256:" + "b" * 64  # 태그는 이미 새 이미지(DIGEST)로 옮겨졌지만 실제로 도는 건 이전 이미지
    s = session(ecs={"list_tasks": {"taskArns": ["arn:task/1"]},
                     "describe_tasks": {"tasks": [{"containers": [{"name": "app", "imageDigest": running}]}]}})
    res = app_part.collect(_inp(), s)
    assert res.facts.image_digest == running
    assert all(name != "describe_images" for name, _ in s.clients["ecr"].calls)


def test_two_running_versions_are_not_pinned():
    s = session(ecs={"list_tasks": {"taskArns": ["arn:task/1", "arn:task/2"]},
                     "describe_tasks": {"tasks": [
                         {"containers": [{"name": "app", "imageDigest": "sha256:" + "b" * 64}]},
                         {"containers": [{"name": "app", "imageDigest": "sha256:" + "c" * 64}]},
                     ]}})
    res = app_part.collect(_inp(), s)
    assert res.facts.image_digest is None
    assert any("2개 버전" in g for g in res.gaps)


def test_no_running_task_falls_back_to_tag_lookup():
    res = app_part.collect(_inp(), session())
    assert res.facts.image_digest == DIGEST


# -- 비밀 이름 환경 변수: provisioning 이 채우는 이름만 조용히, 나머지는 승인자에게 알림 (#79) ----------------

INJECTED_SECRET_NAMES = ["PGPASSWORD"]
OTHER_SECRET_NAMES = [
    "DB_PASSWORD", "DB_PASSWD", "CLIENT_SECRET", "ACCESS_TOKEN", "SIGNING_KEY", "KEY", "PASSPHRASE",
    "SSH_PRIVATE_KEY", "SERVICE_API_KEY", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS", "API_KEY",
]


def _with_env(name: str, value: str):
    c = container()
    c["environment"].append({"name": name, "value": value})
    return c


@pytest.mark.parametrize("name", INJECTED_SECRET_NAMES)
def test_injected_secret_name_is_dropped_without_a_gap(name):
    res = app_part.collect(_inp(), session(_with_env(name, "sentinel-value")))
    assert name not in res.facts.env_vars
    assert "sentinel-value" not in res.model_dump_json()
    assert not any(name in g for g in res.gaps)


@pytest.mark.parametrize("name", OTHER_SECRET_NAMES)
def test_other_secret_names_keep_their_warning_and_never_leak_the_value(name):
    res = app_part.collect(_inp(), session(_with_env(name, "sentinel-value")))
    assert name not in res.facts.env_vars
    assert "sentinel-value" not in res.model_dump_json()
    assert any(name in g and "Secrets Manager" in g for g in res.gaps)


def test_credentials_in_a_value_keep_their_warning_even_for_a_plain_name():
    res = app_part.collect(_inp(), session(_with_env("DATABASE_URL", "postgres://u:pl41n@h/db")))
    assert "DATABASE_URL" not in res.facts.env_vars
    assert any("DATABASE_URL" in g for g in res.gaps)
    assert "pl41n" not in res.model_dump_json()


def test_dropping_an_injected_secret_does_not_hide_a_real_failure():
    res = app_part.collect(
        _inp(), session(_with_env("PGPASSWORD", "sentinel-value"), ecr={"describe_images": FakeAwsError("AccessDeniedException")})
    )
    assert not any("PGPASSWORD" in g for g in res.gaps)
    assert any("AccessDeniedException" in g for g in res.gaps)


def test_secret_refs_are_untouched():
    c = _with_env("PGPASSWORD", "sentinel-value")
    res = app_part.collect(_inp(), session(c))
    assert res.facts.secret_refs == {item["name"]: item["valueFrom"] for item in c["secrets"]}


def test_the_plan_tells_the_approver_about_dropped_secrets_but_not_about_pgpassword():
    """승인자가 보는 계획 단위로 확인: 이 경고가 계획에서 사라지면 안 됩니다."""
    from app.features.assessment import plan as plan_part
    from app.features.assessment.schemas import DbInfo, InventoryForPlanning, PlanningInput, TargetSelection

    c = _with_env("PGPASSWORD", "x")
    c["environment"] += [{"name": "API_KEY", "value": "k"}, {"name": "DB_PASSWORD", "value": "p"}]
    res = app_part.collect(_inp(), session(c))
    db = DbInfo(engine="postgres", engine_version="16.4", database_name="app", logical_replication=True,
                username="alpha", tables=[{"name": "t", "has_primary_key": True}])
    plan = plan_part.build(PlanningInput(
        inventory=InventoryForPlanning(app=res.facts, db=db, gaps=res.gaps),
        target=TargetSelection(region="asia-northeast3"),
    ))
    text = " ".join(r.title + " " + r.detail for r in plan.risks)
    assert "API_KEY" in text and "DB_PASSWORD" in text
    assert "PGPASSWORD" not in text
