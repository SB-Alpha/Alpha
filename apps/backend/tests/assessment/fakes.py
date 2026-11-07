"""AWS 를 부르지 않고 app.py · service.py 를 확인하기 위한 가짜 세션.

계정 ID·ARN 은 전부 예시 값입니다 (실제 원본 계정 아님).
"""

from __future__ import annotations

ACCOUNT = "123456789012"
IMAGE = f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com/target-app:abc123"
DIGEST = "sha256:" + "a" * 64


class FakeAwsError(Exception):
    """botocore ClientError 와 같은 모양 (response["Error"]["Code"])."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "secret-looking message must not leak"}}


class FakeClient:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            res = self.responses[name]  # 없으면 KeyError → 조회 실패로 처리되는지 확인
            if isinstance(res, Exception):
                raise res
            return res

        return method


class FakeSession:
    def __init__(self, **clients: FakeClient):
        self.clients = clients
        self.regions: dict[str, str | None] = {}

    def client(self, service_name: str, region_name: str | None = None):
        self.regions[service_name] = region_name
        return self.clients.setdefault(service_name, FakeClient({}))


def container(**overrides) -> dict:
    c = {
        "name": "app",
        "image": IMAGE,
        "essential": True,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        "environment": [
            {"name": "LOG_LEVEL", "value": "INFO"},
            {"name": "PGUSER", "value": "alpha"},
            {"name": "PGHOST", "value": "db.example.internal"},
            {"name": "DB_PASSWORD", "value": "s3cr3t-plain"},
        ],
        "secrets": [{"name": "PGPASSWORD", "valueFrom": f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:pg"}],
        "healthCheck": {"command": ["CMD-SHELL", "curl -f http://localhost:8080/health/ready || exit 1"]},
    }
    c.update(overrides)
    return c


def session(container_def: dict | None = None, *, service: dict | None = None, **overrides) -> FakeSession:
    svc = service or {"serviceName": "target-app", "taskDefinition": "arn:task/target-app:7", "loadBalancers": []}
    ecs = {
        "list_clusters": {"clusterArns": ["arn:cluster/alpha"]},
        "list_services": {"serviceArns": ["arn:service/alpha/target-app"]},
        "describe_services": {"services": [svc]},
        "list_tasks": {"taskArns": []},
        "describe_task_definition": {"taskDefinition": {
            "cpu": "512", "memory": "1024", "containerDefinitions": [container_def or container()],
        }},
    }
    ecs.update(overrides.pop("ecs", {}))
    return FakeSession(
        sts=FakeClient({"get_caller_identity": {"Account": ACCOUNT}}),
        ecs=FakeClient(ecs),
        ecr=FakeClient(overrides.pop("ecr", {"describe_images": {"imageDetails": [{"imageDigest": DIGEST}]}})),
        elbv2=FakeClient(overrides.pop("elbv2", {})),
    )
