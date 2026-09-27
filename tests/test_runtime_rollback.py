"""Cancellation and failure contracts for resource ownership during rollback."""

import asyncio

import pytest
from aiohttp import web
from conftest import free_port

from fault_engine.config import Config
from fault_engine.runtime import Runtime


def make_runtime(upstream):
    return Runtime(
        Config.model_validate(
            {
                "services": [{"id": "orders", "port": free_port(), "upstream": upstream[0]}],
                "admin": {"port": free_port()},
            }
        ),
        admin_token="test-token",
    )


@pytest.mark.parametrize("concurrent_close", [False, True])
async def test_repeated_start_cancellation_keeps_rollback_alive(
    upstream, monkeypatch, concurrent_close
):
    app = make_runtime(upstream)
    other = make_runtime(upstream)
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    original_start = Runtime._start
    original_cleanup = web.AppRunner.cleanup
    cleanup_calls = 0

    async def pause_start(runtime):
        await original_start(runtime)
        started.set()
        await asyncio.Event().wait()

    async def pause_cleanup(runner):
        nonlocal cleanup_calls
        if runner is app.admin:
            cleanup_calls += 1
            cleaning.set()
            await release.wait()
        await original_cleanup(runner)

    monkeypatch.setattr(Runtime, "_start", pause_start)
    monkeypatch.setattr(web.AppRunner, "cleanup", pause_cleanup)
    starting = asyncio.create_task(app.start())
    closing = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        starting.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        if concurrent_close:
            closing = asyncio.create_task(app.close())
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 2)
        assert Runtime._active is app
        with pytest.raises(RuntimeError, match="only one Runtime"):
            await other.start()
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 2)
        else:
            # No later close() call should be needed to finish abandoned rollback.
            async with asyncio.timeout(2):
                while Runtime._active is app:
                    await asyncio.sleep(0.005)
        assert cleanup_calls == 1
        assert app.master is None
        assert app.admin is None
        assert not app.bridges
        assert Runtime._active is None
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        if closing is not None:
            await closing
        await app.close()
        await other.close()


async def test_failed_cleanup_retains_resource_and_ownership_for_retry(upstream, monkeypatch):
    app = make_runtime(upstream)
    other = make_runtime(upstream)
    await app.start()
    admin = app.admin
    original_cleanup = web.AppRunner.cleanup
    failures = 1

    async def fail_once(runner):
        nonlocal failures
        if runner is admin and failures:
            failures -= 1
            raise OSError("temporary cleanup failure")
        await original_cleanup(runner)

    monkeypatch.setattr(web.AppRunner, "cleanup", fail_once)
    try:
        with pytest.raises(OSError, match="temporary cleanup failure"):
            await app.close()
        assert app.admin is admin
        assert Runtime._active is app
        with pytest.raises(RuntimeError, match="only one Runtime"):
            await other.start()
        # A later close retries the exact failed resource, then relinquishes ownership.
        await app.close()
        assert app.admin is None
        assert Runtime._active is None
        await other.start()
    finally:
        await app.close()
        if admin is not None:
            await original_cleanup(admin)
        await other.close()


async def test_startup_error_survives_failed_rollback_and_close_can_retry(upstream, monkeypatch):
    app = make_runtime(upstream)
    original_start = Runtime._start
    original_cleanup = web.AppRunner.cleanup
    failures = 1

    async def fail_after_binding(runtime):
        await original_start(runtime)
        raise ValueError("startup failure")

    async def fail_cleanup_once(runner):
        nonlocal failures
        if runner is app.admin and failures:
            failures -= 1
            raise OSError("rollback failure")
        await original_cleanup(runner)

    monkeypatch.setattr(Runtime, "_start", fail_after_binding)
    monkeypatch.setattr(web.AppRunner, "cleanup", fail_cleanup_once)
    try:
        with pytest.raises(ValueError, match="startup failure") as error:
            await app.start()
        assert isinstance(error.value.__cause__, OSError)
        assert app.admin is not None
        assert Runtime._active is app
        await app.close()
        assert app.admin is None
        assert Runtime._active is None
    finally:
        await app.close()
