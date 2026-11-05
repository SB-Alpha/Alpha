#!/usr/bin/env bash
# 노드 접속 준비: 지금 공인 IP를 Bastion 허용 목록에 넣고, 세션을 새로 받아
# 노드 1·2·3을 localhost:2201·2202·2203으로 포워딩한다.
#   ./bastion.sh          (공인 IP가 바뀌었거나 세션이 만료됐을 때 다시 실행)
set -euo pipefail

key="${SSH_KEY:-$HOME/.ssh/alpha-cdc}"
tf="$(cd "$(dirname "$0")/../terraform/oci" && pwd)"
ip="$(curl -fsS https://ifconfig.me)"

# 만료된 세션은 다시 만든다.
terraform -chdir="$tf" apply -auto-approve -input=false -var "admin_cidrs=[\"$ip/32\"]" \
  -replace='oci_bastion_session.ssh[0]' -replace='oci_bastion_session.ssh[1]' -replace='oci_bastion_session.ssh[2]' >/dev/null

pkill -f 'host.bastion.*oraclecloud.com' 2>/dev/null || true
port=2201
while read -r cmd; do
  cmd="${cmd//<privateKey>/$key}"
  cmd="${cmd//<localPort>/$port}"
  cmd="${cmd/ssh -i/ssh -f -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -i}"
  # 새 세션은 키가 퍼지는 데 몇십 초 걸린다.
  for _ in $(seq 1 12); do eval "$cmd" 2>/dev/null && break; sleep 5; done
  port=$((port + 1))
done < <(terraform -chdir="$tf" output -json ssh_tunnel_commands | python3 -c 'import sys, json; print("\n".join(json.load(sys.stdin)))')

for p in 2201 2202 2203; do
  ssh -i "$key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p "$p" ubuntu@localhost hostname
done
