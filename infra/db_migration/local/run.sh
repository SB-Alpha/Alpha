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
  load)
    # fake-worker 로 쓰기 부하를 보낸다. 끝나면 목적지에 모두 반영됐는지 출력한다.
    compose exec -T fake-worker python load.py "${2:-30}" "${3:-10}"
    ;;
  down)
    compose down -v
    ;;
  *)
    echo "usage: $0 up|load [seconds] [rps]|down" >&2; exit 2;;
esac
