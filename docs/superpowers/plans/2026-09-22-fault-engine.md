# Fault Engine Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for isolated transport and reviews; use test-driven-development for each behavior. All execution goes through `bash .agent/run.sh`.

**Goal:** Deliver the approved HTTP fault engine with real TCP reset, repeatable scenarios, Chinese documentation, and verified tests.

**Architecture:** Pure configuration and decision engine feed a mitmproxy addon. A TCP front end owns public client sockets for precise RST; a separate authenticated admin server manages in-memory state.

**Tech Stack:** Python 3.12, uv, mitmproxy 12, pydantic 2, aiohttp, pytest, ruff, ty.

No Git metadata exists in the supplied empty directory, so work is performed in place. Do not fabricate commits or worktree status.

## Task 1 — Container/toolchain

- [x] Create `.agent/run.sh` with Python 3.12 image and optional localhost-only port publishing.
- [ ] Resolve `pyproject.toml` with uv in Docker; retain uv.lock. Bootstrap uv inside the container only.
- [ ] Verify `python --version`, `uv lock --check`, and editable package import.

## Task 2 — Connection transport

Files: `src/fault_engine/transport.py`, `tests/test_transport.py`.

- [ ] Write TCP echo/half-close, independent-client and reset tests before implementation.
- [ ] Observe the missing behavior with `uv run pytest tests/test_transport.py -q`.
- [ ] Implement `TCPBridge(target_host, target_port)`, `await start(host, port)`, `port`, `reset(peer) -> bool`, `await close()`; `peer` is the local address of the bridge-to-mitmproxy socket, identical to mitmproxy's client peername.
- [ ] Register the mapping before relaying bytes. RST sets SO_LINGER to `(1,0)` then aborts the public transport. Close cancels all relay tasks and releases ports.
- [ ] Verify a real Linux client sees `ConnectionResetError` and errno 104, and another connection still echoes bytes.

## Task 3 — Configuration and pure engine

Files: `src/fault_engine/config.py`, `src/fault_engine/engine.py`, `tests/test_config.py`, `tests/test_engine.py`.

- [ ] Write invalid-config tests and scenario tests. Canonical boundary: start_at=3 and respond(status=429,repeat=2) yields `[pass,pass,429,429,pass]`.
- [ ] Run `uv run pytest tests/test_config.py tests/test_engine.py -q` and capture expected failures.
- [ ] Implement strict pydantic models for services, matching, discriminated actions, scoped rules, state bounds and admin settings. Reject unknown/contradictory fields before binding.
- [ ] Implement `Engine.decide(service, method, path, headers, query) -> Decision | None`, synchronous atomic sequence assignment, explicit missing-scope/capacity errors, TTL notices, `snapshot()` and filtered `reset()`.
- [ ] Test repeat_last/cycle, per-header isolation, method/header/query matching, first-match, TTL and capacity, reset and in-flight decision immutability.

## Task 4 — Network application

Files: `src/fault_engine/addon.py`, `runtime.py`, `admin.py`, `cli.py`; `tests/test_integration.py`, `tests/test_cli.py`.

- [ ] Write real-upstream tests for verb/body/query preservation and retry `429,429,200`, verifying only one upstream call.
- [ ] Observe failure before implementing application startup, service mapping and addon.
- [ ] Implement immutable request decisions and async fault actions. Set connection_strategy=lazy, HTTP/2 disabled for the validated HTTP/1.1 baseline, internal listener loopback only.
- [ ] Implement admin health/state/rules/reset with bearer-token validation, bounded JSON input and filtered reset.
- [ ] Add and run tests before each new action: delays, timeout, disconnect, reset; validate upstream counts and unrelated connection liveness.
- [ ] Verify HEAD/204/304, custom methods, HTTPS upstream, multiple services, upstream errors, shutdown and client cancellation.
- [ ] Implement validate/serve CLI; errors exit nonzero without printing secret config values.

## Task 5 — Delivery and review

Files: `README.md`, `docs/usage.md`, `docs/development.md`, `docs/verification.md`, `examples/scenarios.yaml`, `.agent/check.sh`.

- [ ] Document exact config syntax, state semantics, control-plane security, all actions, actual limitations and extension interfaces.
- [ ] Run the documented startup/curl/reset walkthrough inside Docker, plus package build and installed CLI smoke test.
- [ ] Run `uv lock --check`, `uv run ruff format --check .`, `uv run ruff check .`, `uv run ty check`, full pytest coverage and `uv build`.
- [ ] Request independent spec review followed by quality review; reproduce and fix actionable findings with regression tests.
- [ ] Audit every approved requirement against test evidence in `docs/verification.md`; only declare completion when all required artifacts and gates have evidence.
