#!/usr/bin/env bash
# 노드 3대에 비밀값을 넣는다. 값은 화면에 찍지 않는다. bastion.sh로 포워딩을 먼저 연다.
#   ./push-secrets.sh job <job_id> <properties 파일>   작업 DB 계정
#   ./push-secrets.sh worker-token <파일>              Worker 제어 토큰
#   ./push-secrets.sh tunnel-token                     terraform/cloudflare 출력값을 node.env에
#   ./push-secrets.sh secrets-key                      작업 비밀 암호화 키를 새로 만들어 node.env에 (도는 작업이 없을 때만)
set -euo pipefail

key="${SSH_KEY:-$HOME/.ssh/alpha-cdc}"
ports=(2201 2202 2203)
here="$(cd "$(dirname "$0")" && pwd)"

put_secret() { # <파일명> <로컬 파일>
  for p in "${ports[@]}"; do
    ssh -i "$key" -p "$p" ubuntu@localhost \
      "sudo install -o 1001 -g 1001 -m 600 /dev/stdin /etc/alpha-cdc/secrets/$1" < "$2"
  done
  echo "$1 → ${#ports[@]} nodes"
}

case "${1:-}" in
  job)
    [[ "${2:-}" =~ ^[a-z][a-z0-9_]{1,30}$ ]] || { echo "job_id 형식이 아니다" >&2; exit 2; }
    for k in source.user source.password source.app.user source.app.password \
             source.admin.user source.admin.password target.user target.password target.ddl.user target.ddl.password \
             target.admin.user target.admin.password; do
      grep -q "^$k=." "$3" || { echo "$3 에 $k 가 없다" >&2; exit 2; }
    done
    put_secret "$2.properties" "$3"
    ;;
  worker-token)
    put_secret worker.token "$2"
    ;;
  tunnel-token)
    for p in "${ports[@]}"; do
      terraform -chdir="$here/../terraform/cloudflare" output -raw tunnel_token |
        ssh -i "$key" -p "$p" ubuntu@localhost 'tok=$(cat) &&
          sudo sed -i "/^TUNNEL_TOKEN=/d" /etc/alpha-cdc/node.env &&
          echo "TUNNEL_TOKEN=$tok" | sudo tee -a /etc/alpha-cdc/node.env >/dev/null &&
          sudo chmod 600 /etc/alpha-cdc/node.env'
    done
    echo "TUNNEL_TOKEN → ${#ports[@]} nodes (deploy.sh를 다시 돌리면 cloudflared가 뜬다)"
    ;;
  secrets-key)
    # 3대가 같은 키를 써야 한다. 키는 노드 node.env에만 남는다.
    value="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
    for p in "${ports[@]}"; do
      printf '%s' "$value" | ssh -i "$key" -p "$p" ubuntu@localhost 'val=$(cat) &&
        sudo sed -i "/^SECRETS_KEY=/d" /etc/alpha-cdc/node.env &&
        echo "SECRETS_KEY=$val" | sudo tee -a /etc/alpha-cdc/node.env >/dev/null &&
        sudo chmod 600 /etc/alpha-cdc/node.env'
    done
    echo "SECRETS_KEY → ${#ports[@]} nodes (deploy.sh를 다시 돌리면 적용된다)"
    ;;
  *)
    sed -n '2,7p' "$0" >&2; exit 2
    ;;
esac
