#!/usr/bin/env bash
# Execute only inside .agent/run.sh.
set -euo pipefail
uv lock --check
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov=fault_engine --cov-report=term-missing
uv build
bash .agent/check-wheel.sh
