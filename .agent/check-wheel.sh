#!/usr/bin/env bash
# Execute only inside .agent/run.sh, after uv build.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEEL_CHECK_ROOT="$(mktemp -d)"
trap 'rm -rf "$WHEEL_CHECK_ROOT"' EXIT
cd "$PROJECT_ROOT"
PROJECT_VERSION="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
uv export --locked --no-dev --no-emit-project --no-hashes --quiet \
  --output-file "$WHEEL_CHECK_ROOT/requirements.txt"
uv venv --quiet --python /usr/local/bin/python "$WHEEL_CHECK_ROOT/venv"
uv pip install --quiet --offline --python "$WHEEL_CHECK_ROOT/venv/bin/python" \
  --constraint "$WHEEL_CHECK_ROOT/requirements.txt" \
  "$PROJECT_ROOT/dist/fault_engine-${PROJECT_VERSION}-py3-none-any.whl"
# Running outside the checkout ensures imports come from the installed wheel.
cd "$WHEEL_CHECK_ROOT"
"$WHEEL_CHECK_ROOT/venv/bin/python" "$PROJECT_ROOT/.agent/wheel_smoke.py"
