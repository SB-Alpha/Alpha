#!/usr/bin/env bash
# 로컬 CDC 시험. 비밀번호는 실행할 때마다 새로 만들어 .local/ (커밋 제외)에 둔다.
set -euo pipefail
cd "$(dirname "$0")"
compose() { docker compose --env-file .local/.env "$@"; }
api() { curl -sf "$@"; }

case "${1:-}" in
  up)
    mkdir -p .local/secrets .local/agent-secrets
    rm -f .local/secrets/worker.token
    if [ ! -f .local/.env ]; then
      gen() { openssl rand -hex 16; }
      cat > .local/.env <<ENV
SOURCE_ADMIN_PASSWORD=$(gen)
SOURCE_APP_PASSWORD=$(gen)
SOURCE_REPL_PASSWORD=$(gen)
TARGET_ADMIN_PASSWORD=$(gen)
TARGET_DDL_PASSWORD=$(gen)
TARGET_SINK_PASSWORD=$(gen)
WORKER_TOKEN=$(gen)
ENV
    fi
    grep -q '^SECRETS_KEY=' .local/.env || echo "SECRETS_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .local/.env
    set -a; . .local/.env; set +a
    printf '%s' "$WORKER_TOKEN" > .local/agent-secrets/worker.token
    chmod 644 .local/agent-secrets/worker.token
    compose up -d --build
    for _ in $(seq 1 90); do api localhost:18080/status >/dev/null 2>&1 && break; sleep 2; done
    api localhost:18080/status; echo
    ;;
  e2e)
    seconds="${2:-30}"
    set -a; . .local/.env; set +a
    python3 -c 'import json, os
e = os.environ
print(json.dumps({
    "source.user": "alpha_cdc_repl", "source.password": e["SOURCE_REPL_PASSWORD"],
    "source.app.user": "alpha", "source.app.password": e["SOURCE_APP_PASSWORD"],
    "source.admin.user": "alpha", "source.admin.password": e["SOURCE_APP_PASSWORD"],
    "target.user": "tgt_sink", "target.password": e["TARGET_SINK_PASSWORD"],
    "target.ddl.user": "tgt_ddl", "target.ddl.password": e["TARGET_DDL_PASSWORD"],
    "target.admin.user": "postgres", "target.admin.password": e["TARGET_ADMIN_PASSWORD"]}))' |
      api -X PUT localhost:18080/jobs/job01/secrets -H 'content-type: application/json' -d @- >/dev/null
    api -X POST localhost:18080/jobs -H 'content-type: application/json' -d '{
      "job_id": "job01",
      "source": {"host": "source-db", "database": "postgres",
                 "tables": ["public.work_items"], "app_role": "alpha"},
      "target": {"host": "target-db", "database": "app", "app_role": "tgt_app"}
    }' >/dev/null
    echo "작업 생성. 동기화 대기"
    for _ in $(seq 1 120); do
      s=$(api localhost:18080/jobs/job01 | python3 -c 'import sys,json;j=json.load(sys.stdin);print(j["state"],j["phase"],j["message"])')
      echo "  $s"
      case "$s" in waiting_approval*|failed*) break;; esac
      sleep 3
    done
    case "$s" in failed*) exit 1;; esac
    echo "부하 시작 (${seconds}초, 10 rps). 10초 뒤 전환"
    compose exec -T fake-worker python load.py "$seconds" 10 > .local/load.out &
    load=$!
    sleep 10
    api -X POST localhost:18080/jobs/job01/cutover -H 'content-type: application/json' -d '{"approved_by":"local-e2e"}' >/dev/null
    for _ in $(seq 1 30); do
      s=$(api localhost:18080/jobs/job01 | python3 -c 'import sys,json;j=json.load(sys.stdin);print(j["state"],j["phase"])')
      case "$s" in "running cutover") sleep 1;; *) break;; esac
    done
    wait $load
    echo "== 작업"; api localhost:18080/jobs/job01 | python3 -c '
import sys,json; j=json.load(sys.stdin); c=j["evidence"].get("cutover",{}); v=j["evidence"].get("validation",{})
print("state:", j["state"], j["phase"], "-", j["message"])
print("hold_ms:", c.get("hold_ms"), "held_count:", c.get("held_count"))
for s in c.get("steps",[]): print("  ", s)
print("validation mismatch:", v.get("mismatch"), "rows:", v.get("rows_checked"))'
    echo "== 부하"; cat .local/load.out
    ;;
  run)
    # backend run·stop 전체 흐름 (VPN은 건너뜀). backend 의존성이 있는 python으로 실행한다.
    "${PYTHON:-python3}" e2e_run.py
    ;;
  flow)
    # db_workflow 전체 흐름: run → cutover → finish (VPN은 건너뜀).
    "${PYTHON:-python3}" e2e_flow.py
    ;;
  workflow)
    # workflows 엔진·컷오버 전체 흐름 (클라우드 없음). serve 를 띄운 뒤 다른 터미널에서 prepare·verify 를 부른다.
    shift
    "${PYTHON:-python3}" e2e_workflow.py "$@"
    ;;
  load)
    # fake-worker 로 쓰기 부하를 보낸다. 끝나면 목적지에 모두 반영됐는지 출력한다.
    compose exec -T fake-worker python load.py "${2:-30}" "${3:-10}"
    ;;
  down)
    compose down -v
    ;;
  *)
    echo "usage: $0 up|e2e [seconds]|run|flow|workflow serve|prepare|verify <run_id>|load [seconds] [rps]|down" >&2; exit 2;;
esac
