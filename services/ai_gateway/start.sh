#!/bin/bash
# Start from the repository's existing virtual environment and shell config.
set -euo pipefail
gateway_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$gateway_root"
gateway_python="$gateway_root/.venv/bin/python"
if [[ ! -x "$gateway_python" ]]; then
  echo "缺少项目 .venv，请按 services/ai_gateway/BUILD.md 准备环境。" >&2
  exit 1
fi
for gateway_variable in PKA_AI_PROVIDER_KEY PKA_AI_MODEL PKA_AI_GATEWAY_TOKEN; do
  if [[ -z "${!gateway_variable:-}" ]]; then
    echo "缺少环境变量：${gateway_variable}。请在原来已配置网关的终端运行；不要把密钥发到聊天中。" >&2
    exit 1
  fi
done
export PYTHONPATH="$gateway_root/services/api:$gateway_root${PYTHONPATH:+:$PYTHONPATH}"
exec "$gateway_python" -m uvicorn 'services.ai_gateway.main:create_app' \
  --factory --host 127.0.0.1 --port 8091 --workers 1 "$@"
