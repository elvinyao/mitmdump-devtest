#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/tools/bin:$PATH"
export PYTHONPATH="/opt/tools${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -x /opt/tools/bin/uv ]]; then
  python -m pip install --disable-pip-version-check --root-user-action=ignore \
    --target /opt/tools uv==0.12.17
fi
# Login shells reset PATH; keep the documented `sh -lc` interface usable.
ln -sf /opt/tools/bin/uv /usr/local/bin/uv
ln -sf /opt/tools/bin/uvx /usr/local/bin/uvx
exec "$@"
