#!/usr/bin/env bash
# 노드 1대에 compose 파일을 올리고 실행한다.
#   ./deploy.sh <ssh 대상>         예: ./deploy.sh "-p 2201 ubuntu@localhost"
# ssh 대상은 Bastion 포트 포워딩으로 열어 둔 localhost 포트를 가리킨다.
set -euo pipefail

target="$1"
key="${SSH_KEY:-$HOME/.ssh/alpha-cdc}"
here="$(cd "$(dirname "$0")" && pwd)"

# shellcheck disable=SC2086
ssh -i "$key" -o StrictHostKeyChecking=accept-new $target \
  'sudo mkdir -p /opt/alpha-cdc /var/lib/alpha-cdc/kafka /etc/alpha-cdc/secrets /etc/alpha-cdc/agent-secrets && sudo chown -R 1000:1000 /var/lib/alpha-cdc/kafka && sudo chown 1001:1001 /etc/alpha-cdc/secrets /etc/alpha-cdc/agent-secrets && sudo chmod 700 /etc/alpha-cdc/secrets /etc/alpha-cdc/agent-secrets && if sudo test -f /etc/alpha-cdc/secrets/worker.token; then sudo mv /etc/alpha-cdc/secrets/worker.token /etc/alpha-cdc/agent-secrets/worker.token; fi'
# heap을 노드 메모리에 맞춘다. 용량을 키운 뒤 다시 돌리면 그대로 커진다.
# shellcheck disable=SC2086
ssh -i "$key" $target 'mem=$(awk "/MemTotal/ {print int(\$2/1024)}" /proc/meminfo) &&
  sudo sed -i "/^KAFKA_HEAP_MB=/d; /^CONNECT_HEAP_MB=/d" /etc/alpha-cdc/node.env &&
  printf "KAFKA_HEAP_MB=%d\nCONNECT_HEAP_MB=%d\n" $((mem / 5)) $((mem * 3 / 10)) | sudo tee -a /etc/alpha-cdc/node.env'
# shellcheck disable=SC2086
ssh -i "$key" $target 'sudo tee /opt/alpha-cdc/docker-compose.yml >/dev/null' < "$here/docker-compose.yml"
# 에이전트 이미지 소스 (agent + 커넥터 템플릿)
# shellcheck disable=SC2086
tar -C "$here/.." --exclude '__pycache__' --exclude 'tests' -czf - agent connectors \
  | ssh -i "$key" $target 'sudo rm -rf /opt/alpha-cdc/src && sudo mkdir -p /opt/alpha-cdc/src && sudo tar -xzf - -C /opt/alpha-cdc/src'
# 온프렘 목적지: ONPREM_DB_HOSTNAME을 주고 실행하면 node.env에 넣는다. 레포가 공개라 값은 코드에 두지 않는다.
if [ -n "${ONPREM_DB_HOSTNAME:-}" ]; then
  # shellcheck disable=SC2086
  ssh -i "$key" $target "sudo sed -i '/^ONPREM_DB_/d' /etc/alpha-cdc/node.env &&
    printf 'ONPREM_DB_HOSTNAME=%s\nONPREM_DB_LOCAL_PORT=%s\n' '$ONPREM_DB_HOSTNAME' '${ONPREM_DB_LOCAL_PORT:-15432}' | sudo tee -a /etc/alpha-cdc/node.env >/dev/null"
fi
# node.env에 TUNNEL_TOKEN이 있으면 cloudflared도, ONPREM_DB_HOSTNAME이 있으면 온프렘 터널도 띄운다.
# grep이 못 찾아도 up까지 가도록 치환 끝에 true를 둔다.
# shellcheck disable=SC2086
ssh -i "$key" $target 'cd /opt/alpha-cdc &&
  p=$(sudo grep -q "^TUNNEL_TOKEN=." /etc/alpha-cdc/node.env && echo "--profile tunnel"; true) &&
  o=$(sudo grep -q "^ONPREM_DB_HOSTNAME=." /etc/alpha-cdc/node.env && echo "--profile onprem-target"; true) &&
  sudo docker compose --env-file /etc/alpha-cdc/node.env $p $o up -d --build && sudo docker compose --env-file /etc/alpha-cdc/node.env $p $o ps'
