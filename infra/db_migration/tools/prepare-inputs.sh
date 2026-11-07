#!/usr/bin/env bash
# Run in an actual terminal so passwords stay hidden and Enter can copy JSON.
set -euo pipefail
backend_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../apps/backend" && pwd)"
cd "$backend_dir"
python_bin="${CDC_INPUTS_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  if [[ -x "$backend_dir/.venv/bin/python" ]]; then
    python_bin="$backend_dir/.venv/bin/python"
  elif [[ -x "$backend_dir/../../.venv/bin/python" ]]; then
    python_bin="$backend_dir/../../.venv/bin/python"
  else
    python_bin="python3"
  fi
fi
if ! "$python_bin" -c 'import sys; assert sys.version_info >= (3, 11); import pydantic, cryptography, httpx, boto3, google.auth' >/dev/null 2>&1; then
  echo 'Backend 의존성을 설치한 Python 3.11+가 필요합니다. CDC_INPUTS_PYTHON으로 해당 Python 경로를 지정하세요.' >&2
  exit 1
fi
exec "$python_bin" -m app.features.db_migration.prepare_inputs "$@"
