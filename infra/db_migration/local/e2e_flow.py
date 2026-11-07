"""db_workflow 전체 흐름을 로컬 구성으로 시험한다 (VPN만 건너뛴다).

    ./run.sh down && ./run.sh up && PYTHON=<backend 의존성이 있는 python> ./run.sh flow
"""

import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "apps" / "backend"))
ENV = dict(line.split("=", 1) for line in (HERE / ".local" / ".env").read_text().splitlines() if "=" in line)

os.environ.update(
    CDC_AGENT_URL="http://localhost:18080",
    ALPHA_DATA_DIR=tempfile.mkdtemp(prefix="alpha-e2e-"),
    ALPHA_SECRETS_KEY=Fernet.generate_key().decode(),
    CDC_LOG_INTERVAL_S="5",
)

from app.core.secrets import has_secret, put_secret  # noqa: E402
from app.features.db_migration import db_workflow  # noqa: E402
from app.features.db_migration.client import AgentSettings, CdcAgentClient  # noqa: E402

RUN_ID = "mig-flow-" + time.strftime("%H%M%S")
JOB_ID = RUN_ID.replace("-", "_")
REGION = "ap-test-1"
failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "--env-file", ".local/.env", *args],
                          cwd=HERE, capture_output=True, text=True, timeout=120)


def target(query: str) -> str:
    done = compose("exec", "-T", "target-db", "psql", "-U", "postgres", "-d", "app", "-tAc", query)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def source(query: str) -> str:
    done = compose("exec", "-T", "source-db", "psql", "-U", "postgres", "-d", "postgres", "-tAc", query)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def wait(what: str, condition, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        status = db_workflow.status(RUN_ID)
        if status.state == "failed":
            raise RuntimeError(f"{status.phase}: {status.message}")
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(2)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    put_secret(f"{RUN_ID}/target-admin", {"user": "postgres", "password": ENV["TARGET_ADMIN_PASSWORD"]})
    start = {
        "run_id": RUN_ID, "region": REGION, "aws_access_key_id": "unused", "aws_secret_access_key": "unused",
        "db_user": "alpha", "db_password": ENV["SOURCE_APP_PASSWORD"],
        "source": {
            "account_id": "local", "region": REGION,
            "db": {"host": "source-db", "database": "postgres"},
            "network": {"vpc_id": "local", "cidr": "172.16.0.0/12", "route_table_ids": ["local"]},
            "app": {"db_user": "alpha"},
            "tables": ["public.work_items", "public.tickets"],
            "credentials_ref": {"cloud": "unused", "db_admin": "unused"},
        },
        "target": {
            "project_id": "local", "region": "local",
            "db": {"instance": "local", "private_ip": "target-db", "database": "app"},
            "network": {"vpc_name": "local", "subnet_cidrs": ["172.16.0.0/12"], "psa_range_cidr": "172.16.0.0/12"},
            "app": {"db_user": "tgt_app"},
            "credentials_ref": {"cloud": f"secret://{RUN_ID}/target-cloud", "admin": f"secret://{RUN_ID}/target-admin"},
        },
        "options": {"skip_vpn": True},
    }

    # up 직후에는 Connect가 아직 뜨는 중이다.
    agent = CdcAgentClient(AgentSettings.from_env())
    deadline = time.monotonic() + 180
    while not (status := agent.status())["connect"] or status["plugins_missing"]:
        if time.monotonic() > deadline:
            raise TimeoutError("connect")
        time.sleep(2)

    print("== run")
    began = time.monotonic()
    db_workflow.run(start)
    check("run_id를 바로 반환", time.monotonic() - began < 2)
    wait("cutover_ready", lambda: db_workflow.status(RUN_ID).ready_for_cutover)
    check("초기 복제 완료", target("SELECT count(*) FROM public.work_items") != "0")

    print("== 권한 분리")
    connect = f"/etc/alpha-cdc/secrets/{JOB_ID}.properties"
    keys = compose("exec", "-T", "connect", "sh", "-c", f"cut -d= -f1 {connect}").stdout.split()
    check("Connect 파일에는 커넥터 계정만", sorted(keys) == ["source.password", "source.user", "target.password", "target.user"], keys)
    check("Connect에서 에이전트 폴더가 안 보임",
          compose("exec", "-T", "connect", "test", "!", "-e", "/etc/alpha-cdc/agent-secrets").returncode == 0)
    check("Connect에서 Worker 토큰이 안 보임",
          compose("exec", "-T", "connect", "test", "!", "-e", "/etc/alpha-cdc/secrets/worker.token").returncode == 0)
    mode = compose("exec", "-T", "agent", "stat", "-c", "%a", f"/etc/alpha-cdc/agent-secrets/{JOB_ID}.properties").stdout.strip()
    check("에이전트 비밀 파일 권한 600", mode == "600", mode)

    print("== Worker가 없으면 전환 거부")
    compose("stop", "fake-worker")
    try:
        db_workflow.cutover(RUN_ID, "e2e")
        check("전환 거부", False)
    except db_workflow.NotAllowed as exc:
        check("전환 거부", "Worker" in str(exc), exc)
    check("작업은 그대로 대기", db_workflow.status(RUN_ID).ready_for_cutover)
    compose("start", "fake-worker")
    time.sleep(3)

    print("== cutover")
    result = db_workflow.cutover(RUN_ID, "e2e")
    check("전환 성공", result.outcome == "switched", (result.hold_ms, result.detail))
    check("3초 안에 전환", (result.hold_ms or 1e9) < 3000, result.hold_ms)
    check("다시 불러도 같은 결과", db_workflow.cutover(RUN_ID, "e2e").outcome == "switched")
    try:
        db_workflow.stop(RUN_ID)
        check("전환 후 stop 거부", False)
    except db_workflow.NotAllowed:
        check("전환 후 stop 거부", True)

    wait("전환 시점 데이터 비교", lambda: db_workflow.status(RUN_ID).metrics.get("verification") != "running")
    check("전환 시점 데이터 일치", db_workflow.status(RUN_ID).metrics.get("verification") == "match")
    new_id = target("SET ROLE tgt_app; INSERT INTO public.tickets (title) VALUES ('after cutover') RETURNING id;")
    # psql은 명령 태그도 함께 찍는다: SET, 21, INSERT 0 1
    check("전환 후 자동 증가 ID INSERT", "21" in new_id.splitlines(), new_id)

    print("== finish")
    done = db_workflow.finish(RUN_ID)
    check("정리 완료", done.state == "finished", (done.remaining, done.message))
    check("안내 있음", any("IAM" in step for step in done.next_steps))
    check("목적지 계정 NOLOGIN", done.target_accounts.get("retired") is not None
          and target("SELECT count(*) FROM pg_roles WHERE rolname IN ('alpha_ddl','alpha_sink') AND rolcanlogin") == "0",
          done.target_accounts)
    check("앱 계정 로그인 유지", target("SELECT rolcanlogin FROM pg_roles WHERE rolname='tgt_app'") == "t")
    check("복제 슬롯 삭제", source(f"SELECT count(*) FROM pg_replication_slots WHERE slot_name='alpha_cdc_{JOB_ID}'") == "0")
    for path in (connect, f"/etc/alpha-cdc/agent-secrets/{JOB_ID}.properties"):
        check(f"비밀 파일 삭제 {Path(path).parent.name}", compose("exec", "-T", "agent", "test", "!", "-e", path).returncode == 0)
    check("앱의 비밀값 삭제", not has_secret(f"secret://{RUN_ID}/source-db-admin"))
    final = db_workflow.status(RUN_ID)
    check("상태 정리 끝", final.phase == "closed" and not final.remaining and not final.cleanup_required, final.state)

    print("실패:", failures if failures else "없음")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
