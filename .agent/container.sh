#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/tools/bin:$PATH"
export PYTHONPATH="/opt/tools${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -x /opt/tools/bin/uv ]]; then
  python -m pip install --disable-pip-version-check --root-user-action=ignore \
    --target /opt/tools uv==0.12.17
fi
exec "$@"
