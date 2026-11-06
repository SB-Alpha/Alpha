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
