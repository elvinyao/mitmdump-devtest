#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORTS=()
if [[ "${1:-}" == "--publish" ]]; then
  shift
  PORTS=(-p 127.0.0.1:18080:8080 -p 127.0.0.1:18081:8081 -p 127.0.0.1:19090:9090)
fi
exec docker run --rm -i --init \
  -v "${PROJECT_ROOT}:/workspace" \
  -v fault-engine-uv:/root/.cache/uv \
  -v fault-engine-tools:/opt/tools \
  -w /workspace \
  -e UV_PROJECT_ENVIRONMENT=/workspace/.venv-docker \
  -e UV_LINK_MODE=copy \
  ${PORTS[@]+"${PORTS[@]}"} \
  python:3.12-bookworm \
  bash /workspace/.agent/container.sh "$@"
