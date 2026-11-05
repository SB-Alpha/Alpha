"""backend run·stop 전체 흐름을 로컬 구성으로 시험한다 (VPN만 건너뛴다).

    ./run.sh down && ./run.sh up && PYTHON=<backend 의존성이 있는 python> ./run.sh run

입력은 workflows가 넘기는 모양 그대로이고, 비밀은 시험용 SQLite에 넣어 이름표로 넘긴다.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from io import StringIO
from pathlib import Path

from cryptography.fernet import Fernet

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "apps" / "backend"))
ENV = dict(line.split("=", 1) for line in (HERE / ".local" / ".env").read_text().splitlines() if "=" in line)

os.environ.update(
    CDC_AGENT_URL="http://localhost:18080",
    ALPHA_DATA_DIR=tempfile.mkdtemp(prefix="alpha-e2e-"),
    ALPHA_SECRETS_KEY=Fernet.generate_key().decode(),
)

from app.core.secrets import put_secret  # noqa: E402
from app.features.db_migration import run, stop  # noqa: E402
from app.features.db_migration.client import AgentSettings, CdcAgentClient  # noqa: E402
from app.features.db_migration.watch import watch  # noqa: E402

RUN_ID = "mig-e2e-" + time.strftime("%H%M%S")
JOB_ID = RUN_ID.replace("-", "_")
failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "--env-file", ".local/.env", *args],
                          cwd=HERE, capture_output=True, text=True, timeout=60)


def psql(service: str, database: str, query: str) -> str:
    done = compose("exec", "-T", service, "psql", "-U", "postgres", "-d", database, "-tAc", query)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def source(query: str) -> str:
    return psql("source-db", "postgres", query)


def target(query: str) -> str:
    return psql("target-db", "app", query)


def wait(what: str, condition, timeout_s: float = 240) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    refs = {
        tag: put_secret(f"{RUN_ID}/{tag}", value)
        for tag, value in {
            "source-db-admin": {"user": "alpha", "password": ENV["SOURCE_APP_PASSWORD"]},
            "target-admin": {"user": "postgres", "password": ENV["TARGET_ADMIN_PASSWORD"]},
        }.items()
    }
    inp = {
        "run_id": RUN_ID,
        "source": {
            "account_id": "local", "region": "local",
            "db": {"host": "source-db", "database": "postgres"},
            "network": {"vpc_id": "local", "cidr": "172.16.0.0/12", "route_table_ids": ["local"]},
            "app": {"db_user": "alpha"},
            "tables": ["public.work_items"],
            "credentials_ref": {"cloud": "secret://unused", "db_admin": refs["source-db-admin"]},
        },
        "target": {
            "project_id": "local", "region": "local",
            "db": {"instance": "local", "private_ip": "target-db", "database": "app"},
            "network": {"vpc_name": "local", "subnet_cidrs": ["172.16.0.0/12"], "psa_range_cidr": "172.16.0.0/12"},
            "app": {"db_user": "tgt_app"},
            "credentials_ref": {"cloud": "secret://unused", "admin": refs["target-admin"]},
        },
        "options": {"skip_vpn": True, "log_interval_s": 5},
    }
    agent = CdcAgentClient(AgentSettings.from_env())
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(result=run(inp)))
    thread.start()

    def phase() -> str | None:
        try:
            job = agent.get_job(JOB_ID)
        except Exception:
            return None
        if job["state"] == "failed":
            raise RuntimeError(job["message"])
        return job["phase"]

    wait("cutover_ready", lambda: phase() == "cutover_ready")

    print("== 동기화 완료 뒤")
    for role in ("alpha_ddl", "alpha_sink"):
        check(f"{role}: 모듈이 만들고 관리자 아님",
              target(f"SELECT rolsuper FROM pg_roles WHERE rolname='{role}'") == "f")
    check("업무 테이블 소유자 alpha_ddl",
          target("SELECT tableowner FROM pg_tables WHERE tablename='work_items'") == "alpha_ddl")
    check("원본 복제 계정 생성", source("SELECT count(*) FROM pg_roles WHERE rolname='alpha_cdc_repl'") == "1")
    check("publication 생성", source(f"SELECT count(*) FROM pg_publication WHERE pubname='alpha_cdc_{JOB_ID}'") == "1")
    check("초기 행 복사", target("SELECT count(*) FROM public.work_items") == "1000")
    check("목적지 앱 계정 권한",
          target("SELECT has_table_privilege('tgt_app','public.work_items','SELECT,INSERT,UPDATE,DELETE')") == "t")
    mode = compose("exec", "-T", "agent", "stat", "-c", "%a %u", f"/etc/alpha-cdc/secrets/{JOB_ID}.properties").stdout.strip()
    check("노드 비밀 파일 권한 600", mode.startswith("600"), mode)

    source("INSERT INTO work_items (title) VALUES ('after-sync')")
    wait("실시간 반영", lambda: target("SELECT count(*) FROM public.work_items WHERE title='after-sync'") == "1", 60)
    check("실시간 동기화 (INSERT 반영)", True)
    time.sleep(6)  # 지표 로그가 몇 줄 남도록
    job = agent.get_job(JOB_ID)
    check("지표: 불일치 0", job["metrics"].get("mismatch") == 0, job["metrics"].get("rows_checked"))
    check("지표: 복사 100%", job["metrics"].get("copy_percent") == 100,
          job["metrics"].get("tables", {}).get("public.work_items"))
    view = StringIO()
    check("진행상황 화면", watch(RUN_ID, agent, once=True, out=view) == 0 and "1,001 / 1,001행" in view.getvalue(),
          view.getvalue().splitlines()[3:4])

    print("== stop")
    stop(RUN_ID, agent=agent)
    thread.join(30)
    result = holder.get("result")
    check("run 결과 stopped", result is not None and result.state == "stopped", result and result.message)
    check("작업 aborted", agent.get_job(JOB_ID)["state"] == "aborted")
    check("복제 슬롯 삭제", source(f"SELECT count(*) FROM pg_replication_slots WHERE slot_name='alpha_cdc_{JOB_ID}'") == "0")
    check("publication 삭제", source(f"SELECT count(*) FROM pg_publication WHERE pubname='alpha_cdc_{JOB_ID}'") == "0")
    check("복제 계정 삭제", source("SELECT count(*) FROM pg_roles WHERE rolname='alpha_cdc_repl'") == "0")
    check("marker 테이블은 남김", source("SELECT to_regclass('alpha_cdc.cutover_marker') IS NOT NULL") == "t")
    gone = compose("exec", "-T", "agent", "test", "!", "-e", f"/etc/alpha-cdc/secrets/{JOB_ID}.properties")
    check("노드 비밀 파일 삭제", gone.returncode == 0)
    check("목적지 데이터는 남김", target("SELECT count(*) FROM public.work_items") == "1001")
    connectors = subprocess.run(["curl", "-s", "localhost:8083/connectors"], capture_output=True, text=True).stdout
    check("커넥터 삭제", JOB_ID not in connectors, connectors)

    again = run(inp)
    check("끝난 run_id 재실행 거부", again.state == "failed" and "new run_id" in (again.message or ""))

    print("실패:", failures if failures else "없음")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
