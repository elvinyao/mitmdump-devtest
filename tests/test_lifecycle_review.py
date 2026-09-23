"""Exercise shutdown and ownership boundaries with real listeners and clients."""

import asyncio
from contextlib import suppress

import httpx
import pytest
from conftest import free_port, rule

from fault_engine.config import Config
from fault_engine.runtime import Runtime
from fault_engine.transport import TCPBridge


async def test_shutdown_interrupts_unfinished_admin_request_body(proxy):
    async with proxy() as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.admin.port)
        writer.write(
            b"POST /reset HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        await writer.drain()
        assert await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2) == (
            b"HTTP/1.1 100 Continue\r\n\r\n"
        )
        closing = asyncio.create_task(app.close())
        try:
            # A connected client must not postpone process shutdown for aiohttp's
            # default 60-second request grace period.
            await asyncio.wait_for(asyncio.shield(closing), 2)
            assert app.master is None
            assert not app.bridges
        finally:
            writer.transport.abort()
            with suppress(OSError):
                await writer.wait_closed()
            await asyncio.wait_for(closing, 2)


async def test_concurrent_close_is_idempotent(proxy):
    async with proxy() as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            await asyncio.wait_for(asyncio.gather(app.close(), app.close()), 2)
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert app.master is None
            assert app.admin is None
            assert not app.bridges
        finally:
            writer.transport.abort()
            with suppress(OSError):
                await writer.wait_closed()


async def test_second_runtime_is_rejected_without_corrupting_active_runtime(proxy, upstream):
    async with proxy([rule({"action": "respond", "status": 201}, after_sequence="cycle")]) as app:
        other = Runtime(
            Config.model_validate(
                {
                    "services": [{"id": "other", "port": free_port(), "upstream": upstream[0]}],
                    "admin": {"port": free_port()},
                }
            ),
            admin_token="test-token",
        )
        try:
            rejected = False
            try:
                await other.start()
            except RuntimeError:
                rejected = True
            async with httpx.AsyncClient() as client:
                response = await client.get(app.url("orders") + "/fault")
                assert response.status_code == 201, response.text
            assert rejected, "a second runtime must be rejected before mitmproxy globals change"
        finally:
            await other.close()


async def test_shutdown_closes_hung_upstream_and_internal_connection(proxy):
    received = asyncio.Event()
    disconnected = asyncio.Event()
    tasks = set()

    async def hanging_upstream(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            received.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        task = asyncio.create_task(hanging_upstream(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    upstream = await asyncio.start_server(accept, "127.0.0.1", 0)
    target = upstream.sockets[0].getsockname()[1]
    try:
        services = [{"id": "orders", "port": free_port(), "upstream": f"http://127.0.0.1:{target}"}]
        async with proxy(services=services) as app:
            reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
            try:
                writer.write(b"GET /hang HTTP/1.1\r\nHost: test\r\n\r\n")
                await writer.drain()
                await asyncio.wait_for(received.wait(), 2)
                proxyserver = app.proxyserver
                await asyncio.wait_for(app.close(), 2)
                assert await asyncio.wait_for(reader.read(), 1) == b""
                await asyncio.wait_for(disconnected.wait(), 1)
                assert proxyserver is not None
                async with asyncio.timeout(1):
                    while proxyserver.connections:
                        await asyncio.sleep(0.005)
            finally:
                writer.transport.abort()
                with suppress(OSError):
                    await writer.wait_closed()
    finally:
        upstream.close()
        await upstream.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("action", ["delay_before", "delay_after"])
async def test_shutdown_cancels_each_pending_hook_without_leaking_connections(proxy, action):
    async with proxy([rule({"action": action, "seconds": 60.0})]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                while not app.addon.pending:
                    await asyncio.sleep(0.005)
            proxyserver = app.proxyserver
            await asyncio.wait_for(app.close(), 2)
            assert not app.addon.pending
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert proxyserver is not None
            async with asyncio.timeout(1):
                while proxyserver.connections:
                    await asyncio.sleep(0.005)
        finally:
            writer.transport.abort()
            with suppress(OSError):
                await writer.wait_closed()


async def test_cancelled_start_releases_bound_listeners_and_allows_retry(upstream, monkeypatch):
    app = Runtime(
        Config.model_validate(
            {
                "services": [{"id": "orders", "port": free_port(), "upstream": upstream[0]}],
                "admin": {"port": free_port()},
            }
        ),
        admin_token="test-token",
    )
    bound = asyncio.Event()
    original_start = TCPBridge.start

    async def pause_after_binding(bridge, host, port):
        await original_start(bridge, host, port)
        bound.set()
        await asyncio.Event().wait()

    starting = None
    try:
        with monkeypatch.context() as patch:
            # Pause only the lifecycle boundary; actual listener binding is real.
            patch.setattr(TCPBridge, "start", pause_after_binding)
            starting = asyncio.create_task(app.start())
            await asyncio.wait_for(bound.wait(), 2)
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 2)
        assert app.master is None
        assert app.proxyserver is None
        assert app.admin is None
        assert not app.bridges
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        await app.start()
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("orders") + "/retry")).status_code == 200
    finally:
        if starting is not None and not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await app.close()


async def test_cancelled_close_finishes_cleanup_and_remains_idempotent(proxy, monkeypatch):
    async with proxy() as app:
        admin = app.admin
        assert admin is not None
        entered = asyncio.Event()
        release = asyncio.Event()
        original_cleanup = type(admin).cleanup
        cleanup_calls = 0

        async def pause_cleanup(runner):
            nonlocal cleanup_calls
            if runner is admin:
                cleanup_calls += 1
                entered.set()
                await release.wait()
            await original_cleanup(runner)

        with monkeypatch.context() as patch:
            patch.setattr(type(admin), "cleanup", pause_cleanup)
            closing = asyncio.create_task(app.close())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                closing.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, 2)
                # Cleanup must continue even if its caller is cancelled and never
                # calls close again (for example an enclosing asyncio timeout).
                async with asyncio.timeout(2):
                    while app.master is not None:
                        await asyncio.sleep(0.005)
                assert not app.bridges
                assert app.admin is None
                await app.close()
                assert cleanup_calls == 1
            finally:
                release.set()
                await app.close()


async def test_start_queued_after_idle_cleanup_retains_process_ownership(upstream):
    app = Runtime(
        Config.model_validate(
            {
                "services": [{"id": "orders", "port": free_port(), "upstream": upstream[0]}],
                "admin": {"port": free_port()},
            }
        ),
        admin_token="test-token",
    )
    # Queue both operations at their lifecycle boundary, in deterministic order.
    await app._lifecycle_lock.acquire()
    closing = asyncio.create_task(app.close())
    try:
        while app._close_task is None:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        starting = asyncio.create_task(app.start())
        await asyncio.sleep(0)
    finally:
        app._lifecycle_lock.release()
    try:
        await asyncio.wait_for(asyncio.gather(closing, starting), 3)
        assert Runtime._active is app
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("orders") + "/normal")).status_code == 200
    finally:
        await app.close()
