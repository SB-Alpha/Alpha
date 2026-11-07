"""workflows 엔진과 컷오버 전체 흐름을 로컬 구성으로 리허설한다. 클라우드에 닿지 않는다.

    ./run.sh up
    PYTHON=<backend 의존성이 있는 python> ./run.sh workflow serve     # 실제 API, :8010
    PYTHON=... ./run.sh workflow prepare                             # 승인 ② 앞까지 진행, run_id 출력
    (화면이나 API 로 전환 승인, 필요하면 ./run.sh load 로 쓰기 부하)
    PYTHON=... ./run.sh workflow verify <run_id>

discovery·db_migration·cutover 는 실제 runner 이고 실제 에이전트·Debezium·Kafka·fake-worker 를 쓴다.
planning·provisioning·app_deploy 는 runner 를 등록하지 않아 수동 입력으로만 넘어가므로
AWS·GCP·Terraform 이 호출될 경로가 없다. 그래서 목적지 앱 상태는 판정표에 "미제공"으로 나온다.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "apps" / "backend"))

API = "http://127.0.0.1:8010"
API_PORT = 8010
SECRET_PREFIX = "rehearsal"
SOURCE_ADMIN_REF = f"secret://{SECRET_PREFIX}/source-db-admin"
TARGET_ADMIN_REF = f"secret://{SECRET_PREFIX}/target-admin"
# fake-worker 의 TARGET_DSN 이 쓰는 계정과 같아야 전환 뒤 요청이 목적지에 닿는다.
TARGET_DDL_REF = f"secret://{SECRET_PREFIX}/target-ddl"
TARGET_SINK_REF = f"secret://{SECRET_PREFIX}/target-sink"
SOURCE = {
    "account_id": "local",
    "region": "local",
    "db": {"host": "source-db", "database": "postgres"},
    "network": {"vpc_id": "local", "cidr": "172.16.0.0/12", "route_table_ids": ["local"]},
    "app": {"db_user": "alpha"},
    "tables": ["public.work_items"],
    "credentials_ref": {"cloud": "secret://unused", "db_admin": SOURCE_ADMIN_REF},
}
TARGET = {
    "project_id": "local",
    "region": "local",
    "db": {"instance": "local", "private_ip": "target-db", "database": "app"},
    "network": {"vpc_name": "local", "subnet_cidrs": ["172.16.0.0/12"], "psa_range_cidr": "172.16.0.0/12"},
    "app": {"db_user": "tgt_app"},
    "credentials_ref": {
        "cloud": "secret://unused",
        "admin": TARGET_ADMIN_REF,
        "ddl_user": TARGET_DDL_REF,
        "sink_user": TARGET_SINK_REF,
    },
}
PLAN = {"summary": ["로컬 리허설"], "checks": [], "approvable": True, "target_infra": {}}
REQUEST = {
    "github_repo_url": "https://github.com/example/local-rehearsal",
    "aws_region": "local",
    "aws_credentials_ref": "secret://unused",
}


def serve() -> int:
    from cryptography.fernet import Fernet

    env = dict(line.split("=", 1) for line in (HERE / ".local" / ".env").read_text().splitlines() if "=" in line)
    data_dir = tempfile.mkdtemp(prefix="alpha-rehearsal-")
    os.environ.pop("ALPHA_CDC_SIMULATION", None)
    os.environ.update(
        CDC_AGENT_URL="http://localhost:18080",
        WORKER_URL="http://localhost:18081",
        WORKER_TOKEN_FILE=str(HERE / ".local" / "secrets" / "worker.token"),
        ALPHA_PROBE_URL="http://localhost:18081/health/ready",
        ALPHA_DATA_DIR=data_dir,
        ALPHA_SECRETS_KEY=Fernet.generate_key().decode(),
    )

    import uvicorn

    from app.core.secrets import put_secret
    from app.features.db_migration import service
    from app.features.workflows.engine import Engine
    from app.features.workflows.router import get_engine
    from app.features.workflows.runners import clean_up_cdc, run_cutover, run_db_migration, run_discovery
    from app.features.workflows.schemas import StageName
    from app.features.workflows.store import RunStore
    from app.main import app

    put_secret(SOURCE_ADMIN_REF, {"user": "alpha", "password": env["SOURCE_APP_PASSWORD"]})
    put_secret(TARGET_ADMIN_REF, {"user": "postgres", "password": env["TARGET_ADMIN_PASSWORD"]})
    put_secret(TARGET_DDL_REF, {"user": "tgt_ddl", "password": env["TARGET_DDL_PASSWORD"]})
    put_secret(TARGET_SINK_REF, {"user": "tgt_sink", "password": env["TARGET_SINK_PASSWORD"]})

    run_cdc = service.run

    def run_without_vpn(inp: dict, ctx=None, **kwargs):
        return run_cdc({**inp, "options": {"skip_vpn": True, "log_interval_s": 5}}, ctx, **kwargs)

    service.run = run_without_vpn

    engine = Engine(
        RunStore(Path(data_dir) / "workflows.sqlite3"),
        {
            StageName.DISCOVERY: run_discovery,
            StageName.DB_MIGRATION: run_db_migration,
            StageName.CUTOVER: run_cutover,
        },
        abort_cleanup=clean_up_cdc,
    )
    app.dependency_overrides[get_engine] = lambda: engine
    uvicorn.run(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    return 0


def _post(http: httpx.Client, path: str, body: dict | None = None) -> dict:
    response = http.post(path, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"{path}: {response.status_code} {response.text}")
    return response.json()


def _wait_for(http: httpx.Client, run_id: str, what: str, condition, timeout_s: float = 300) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        run = http.get(f"/runs/{run_id}").json()
        if condition(run):
            return run
        if run["state"] == "failed":
            raise RuntimeError(f"{what} 대기 중 실패: {run['message']}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what}: {run['state']} {run['message']}")
        time.sleep(1)


def _manual(http: httpx.Client, run_id: str, stage: str, output: dict) -> None:
    _wait_for(http, run_id, f"{stage} 수동 입력 차례", lambda r: r["state"] == "paused" and r["current_stage"] == stage)
    _post(http, f"/runs/{run_id}/stages/{stage}/manual", {"output": output, "note": "로컬 리허설"})


def _is_synced(run: dict) -> bool:
    readiness = run.get("cutover_readiness")
    if run["waiting_gate"] != "cutover" or not readiness:
        return False
    statuses = {c["id"]: c["status"] for c in readiness["checks"]}
    metrics = next(s for s in run["stages"] if s["stage"] == "db_migration")["metrics"]
    return metrics.get("phase") == "cutover_ready" and all(
        statuses[check] == "pass" for check in ("cdc", "copy", "lag", "mismatch")
    )


def prepare() -> int:
    with httpx.Client(base_url=API, timeout=30) as http:
        run_id = _post(http, "/runs", REQUEST)["run_id"]
        _manual(http, run_id, "planning", {"source": SOURCE, "plan": PLAN, "gaps": []})
        _wait_for(http, run_id, "승인 ①", lambda r: r["waiting_gate"] == "plan")
        _post(http, f"/runs/{run_id}/approve", {"gate": "plan"})
        _manual(http, run_id, "provisioning", {"target": TARGET})
        _manual(http, run_id, "app_deploy", {"note": "로컬 리허설이라 앱 배포 없음"})
        run = _wait_for(http, run_id, "복제 동기화", _is_synced)
    print(run_id)
    for check in run["cutover_readiness"]["checks"]:
        print(f"  {check['status']:<8}{check['title']}: {check['detail']}", file=sys.stderr)
    return 0


def verify(run_id: str) -> int:
    with httpx.Client(base_url=API, timeout=30) as http:
        run = http.get(f"/runs/{run_id}").json()
    metrics = next(s for s in run["stages"] if s["stage"] == "cutover")["metrics"]
    checks = {
        "run 완료": run["state"] == "succeeded",
        "트래픽이 목적지로 전환": metrics.get("serving") == "gcp",
        "전환 시간 측정": isinstance(metrics.get("cutover_seconds"), (int, float)),
        "프로버 실측": isinstance(metrics.get("probe_requests"), int) and metrics["probe_requests"] > 0,
        "프로버 마지막 응답 출처 TARGET": metrics.get("probe_last_origin") == "TARGET",
        "Worker 쓰기 집계": isinstance(metrics.get("write_requests"), int),
    }
    for name, is_ok in checks.items():
        print(f"  {'ok  ' if is_ok else 'FAIL'} {name}")
    keys = ("serving", "cutover_seconds", "failed_requests", "probe_requests", "probe_p99_ms", "probe_max_ms",
            "probe_last_origin", "write_requests", "write_failed_requests")
    print({key: metrics.get(key) for key in keys})
    return 0 if all(checks.values()) else 1


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "serve":
        return serve()
    if command == "prepare":
        return prepare()
    if command == "verify" and len(argv) == 2:
        return verify(argv[1])
    print("usage: e2e_workflow.py serve | prepare | verify <run_id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
