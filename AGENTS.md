# Repository Guidelines

## Project Structure & Module Organization

Fault Engine is a mitmproxy-based reverse proxy for development fault testing.

- `src/fault_engine/`: configuration (`config.py`), decisions (`engine.py`), HTTP hooks (`addon.py`), TCP transport (`transport.py`), runtime, admin API, and CLI.
- `tests/`: unit, integration, socket, and CLI tests; shared fixtures in `conftest.py`.
- `examples/`: runnable `demo.py` and the YAML scenario catalog.
- `docs/`: usage, architecture, and verification records.
- `.agent/`: Docker runner and quality checks. `dist/` contains ignored build artifacts.

## Docker Execution Policy

Use the host only for read-only inspection and source editing through file-editing tools. Do not use host shell redirection for project work. Run every dependency, formatting, testing, build, script, and server command through `bash .agent/run.sh`; never invoke project tools or `docker run` directly on the host.

The runner uses `python:3.12-bookworm` and mounts the repository at `/workspace`. Put compound commands inside the container: `bash .agent/run.sh sh -lc 'command1 && command2'`. If Docker or the runner fails, report the failure; never fall back to host execution.

## Build, Test, and Development Commands

- `bash .agent/run.sh uv sync --locked`: install locked dependencies.
- `bash .agent/run.sh --publish uv run python examples/demo.py`: start backends and proxy; host ports are 18080, 18081, and admin 19090.
- `bash .agent/run.sh uv run pytest tests/test_engine.py -q`: run focused tests.
- `bash .agent/run.sh uv run ruff format .`: format Python files.
- `bash .agent/run.sh bash .agent/check.sh`: verify lockfile, formatting, Ruff lint, ty types, pytest coverage, and wheel/sdist builds.

## Coding Style & Naming Conventions

Use Python 3.12, four-space indentation, type annotations, and Ruff's 100-character line limit. Use `snake_case` for modules/functions and `PascalCase` for classes. Keep `Engine.decide()` synchronous within one event loop; execute network awaits after selecting a decision.

## Testing Guidelines

Use pytest and pytest-asyncio (automatic asyncio mode). Name files `test_<area>.py` and functions `test_<behavior>`. Add regression tests for behavior changes. Preserve real TCP reset assertions (`ECONNRESET`, errno 104) and backend-call checks. Coverage includes branches and subprocesses; no minimum percentage is enforced. Run the full check before submitting.

## Commit & Pull Request Guidelines

Recent commits use concise `feat: ...` subjects; follow that style with an appropriate change-type prefix. PRs should explain behavior changes, link relevant issues, and report verification results. Update usage documentation and scenario examples when behavior changes.

## Security & Configuration

Keep admin tokens out of Git; configure them through `admin.token_env` (default `FAULT_ADMIN_TOKEN`). Preserve loopback-only host port publishing. Use only for development testing.
