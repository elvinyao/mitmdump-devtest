#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: bash .agent/run.sh [--publish] <command> [args...]' \
    'Run project commands in Docker with Python 3.12.' \
    '  --publish  Bind localhost ports 18080, 18081, and 19090 for the demo.' \
    'Examples:' \
    '  bash .agent/run.sh uv sync --locked' \
    '  bash .agent/run.sh bash .agent/check.sh' \
    '  bash .agent/run.sh --publish uv run python examples/demo.py'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
PORTS=()
if [[ "${1:-}" == "--publish" ]]; then
  shift
  PORTS=(-p 127.0.0.1:18080:8080 -p 127.0.0.1:18081:8081 -p 127.0.0.1:19090:9090)
fi
if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'error: Docker CLI not found; install Docker Desktop/OrbStack and start it.' >&2
  exit 127
fi
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
