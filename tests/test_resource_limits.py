import asyncio
import socket
import struct
from contextlib import suppress

import httpx
import pytest
from conftest import free_port, rule

from fault_engine.limits import Capacity, ResourceBudget
from fault_engine.transport import TCPBridge


async def wait_used(capacity, expected):
    async with asyncio.timeout(2):
        while capacity.used != expected:
            await asyncio.sleep(0.005)


async def close_writer(writer):
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()


def test_capacity_lease_release_is_idempotent():
    capacity = Capacity(1)
    first = capacity.acquire()
    assert first is not None
    assert capacity.acquire() is None
    first.release()
    second = capacity.acquire()
    assert second is not None
    first.release()
    assert capacity.used == 1
    second.release()
    second.release()
    assert capacity.used == 0


async def test_connection_limit_is_shared_across_services_and_admin_remains_available(
    proxy, upstream
):
    services = [
        {"id": "orders", "port": free_port(), "upstream": upstream[0]},
        {"id": "inventory", "port": free_port(), "upstream": upstream[0]},
    ]
    async with proxy(services=services, limits={"max_connections": 1}) as app:
        _, first = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            await wait_used(app.budget.connections, 1)
            reader, rejected = await asyncio.open_connection(
                "127.0.0.1", app.config.services[1].port
            )
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await close_writer(rejected)
            async with httpx.AsyncClient() as client:
                assert (await client.get(app.admin_url + "/health")).status_code == 200
            assert upstream[1] == []
        finally:
            await close_writer(first)
        await wait_used(app.budget.connections, 0)
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("inventory") + "/ok")).status_code == 200
    assert app.budget.connections.used == app.budget.requests.used == 0


@pytest.mark.parametrize("rejected_method", [b"POST", b"HEAD"])
async def test_slow_upload_occupies_request_capacity_and_rejection_never_consumes_step(
    proxy, upstream, rejected_method
):
    async with proxy(
        [rule({"action": "respond", "status": 201})], limits={"max_inflight_requests": 1}
    ) as app:
        _, slow = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            slow.write(b"POST /fault HTTP/1.1\r\nHost: test\r\nContent-Length: 10\r\n\r\nx")
            await slow.drain()
            await wait_used(app.budget.requests, 1)
            reader, rejected = await asyncio.open_connection(
                "127.0.0.1", app.config.services[0].port
            )
            try:
                rejected.write(
                    rejected_method
                    + b" /fault HTTP/1.1\r\nHost: test\r\nContent-Length: 1000\r\n\r\n"
                )
                await rejected.drain()
                # Rejection cannot wait for the absent request body.
                response = await asyncio.wait_for(reader.read(), 1)
                head, body = response.split(b"\r\n\r\n", 1)
                assert head.startswith(b"HTTP/1.1 503 ")
                assert b"X-Fault-Engine-Error: capacity" in head
                assert b"Connection: close" in head
                assert bool(body) is (rejected_method != b"HEAD")
            finally:
                await close_writer(rejected)
            assert app.engine.snapshot() == []
            assert upstream[1] == []
            assert app.budget.requests.used == 1
        finally:
            await close_writer(slow)
        await wait_used(app.budget.requests, 0)
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("orders") + "/fault")).status_code == 201
        assert app.engine.snapshot()[0]["count"] == 1
        await wait_used(app.budget.requests, 0)


@pytest.mark.parametrize("action", ["delay_before", "delay_after", "respond_after"])
async def test_delayed_actions_hold_request_capacity_until_response_finishes(proxy, action):
    step = (
        {"action": action, "status": 202, "delay_seconds": 0.15}
        if action == "respond_after"
        else {"action": action, "seconds": 0.15}
    )
    async with proxy([rule(step)], limits={"max_inflight_requests": 1}) as app:
        async with httpx.AsyncClient() as slow, httpx.AsyncClient() as fast:
            request = asyncio.create_task(slow.get(app.url("orders") + "/fault"))
            try:
                await wait_used(app.budget.requests, 1)
                response = await fast.get(app.url("orders") + "/fault")
                assert response.status_code == 503
                assert response.headers["x-fault-engine-error"] == "capacity"
                assert (await request).status_code == (202 if action == "respond_after" else 200)
                assert app.engine.snapshot()[0]["count"] == 1
                await wait_used(app.budget.requests, 0)
                assert (await fast.get(app.url("orders") + "/normal")).status_code == 200
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("action", ["reset", "disconnect", "timeout"])
async def test_transport_errors_release_request_lease_once(proxy, action):
    step = {"action": action}
    if action == "timeout":
        step["seconds"] = 0.01
    async with proxy([rule(step)], limits={"max_inflight_requests": 1}) as app:
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.TransportError):
                await client.get(app.url("orders") + "/fault")
            await wait_used(app.budget.requests, 0)
            assert (await client.get(app.url("orders") + "/normal")).status_code == 200
            await wait_used(app.budget.requests, 0)


async def test_shutdown_releases_pending_hook_and_connection_capacity(proxy):
    async with proxy([rule({"action": "timeout", "seconds": 60.0})]) as app:
        _, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        await wait_used(app.budget.requests, 1)
        await app.close()
        assert app.budget.connections.used == app.budget.requests.used == 0
        await app.close()
        assert app.budget.connections.used == app.budget.requests.used == 0
        await close_writer(writer)


async def test_reset_client_cancels_only_its_own_pending_request(proxy):
    async with proxy(
        [rule({"action": "timeout", "seconds": 60.0}, after_sequence="repeat_last")],
        limits={"max_inflight_requests": 2},
    ) as app:
        writers = []
        try:
            for _ in range(2):
                _, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
                writers.append(writer)
                writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
                await writer.drain()
            async with asyncio.timeout(2):
                while len(app.addon.pending) != 2:
                    await asyncio.sleep(0.005)
            writers[0].get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            writers[0].transport.abort()
            await wait_used(app.budget.requests, 1)
            assert len(app.addon.pending) == 1
            assert app.engine.snapshot()[0]["count"] == 2
        finally:
            for writer in writers:
                await close_writer(writer)
    assert app.budget.requests.used == 0


async def test_connection_cancelled_before_relay_starts_returns_lease():
    bridge = TCPBridge("127.0.0.1", 9, budget=ResourceBudget(max_connections=1))

    def accepted(reader, writer):
        bridge._accept(reader, writer)
        for task in bridge._tasks:
            task.cancel()

    listener = await asyncio.start_server(accepted, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", listener.sockets[0].getsockname()[1]
        )
        assert await asyncio.wait_for(reader.read(), 1) == b""
        await wait_used(bridge.budget.connections, 0)
        await close_writer(writer)
        assert not bridge._clients
    finally:
        listener.close()
        await listener.wait_closed()
        await bridge.close()


async def test_capacity_rejection_closes_pipelined_connection_without_dispatching_requests(
    proxy, upstream
):
    async with proxy([rule({"action": "respond", "status": 201})]) as app:
        # Reserve all request slots while exercising the real HTTP/1 parser.
        leases = [app.budget.requests.acquire() for _ in range(app.budget.requests.limit)]
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(
                b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n"
                b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1)
            assert response.count(b"HTTP/1.1") == 1
            assert response.startswith(b"HTTP/1.1 503 ")
            assert app.engine.snapshot() == []
            assert upstream[1] == []
        finally:
            await close_writer(writer)
            for lease in leases:
                assert lease is not None
                lease.release()
        await wait_used(app.budget.requests, 0)


async def test_reused_connection_rejection_never_injects_503_into_prior_response(
    proxy, upstream, monkeypatch
):
    scenario = rule({"action": "respond", "status": 200, "body": "x" * (4 * 1024 * 1024)})
    async with proxy([scenario], limits={"max_inflight_requests": 1}) as app:
        occupied = []
        release = app.addon._release_request

        def release_and_reserve(flow_id):
            release(flow_id)
            if not occupied:
                # Deterministically model another client taking the newly free
                # slot before mitmproxy starts the next pipelined request.
                lease = app.budget.requests.acquire()
                assert lease is not None
                occupied.append(lease)

        with monkeypatch.context() as patch:
            patch.setattr(app.addon, "_release_request", release_and_reserve)
            reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
            try:
                writer.write(
                    b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n"
                    b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n"
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                # Closing may truncate the first response. It must never append
                # a second status line inside that response's advertised body.
                assert b"503 Service Unavailable" not in response
                assert b"capacity" not in response
                if response:
                    head, body = response.split(b"\r\n\r\n", 1)
                    assert head.startswith(b"HTTP/1.1 200 ")
                    assert body == b"x" * len(body)
                assert app.engine.snapshot()[0]["count"] == 1
                assert upstream[1] == []
            finally:
                await close_writer(writer)
                for lease in occupied:
                    lease.release()
        await wait_used(app.budget.requests, 0)


async def test_cancelled_rejection_with_stalled_drain_is_bounded_and_releases_capacity(monkeypatch):
    peers = asyncio.Queue()

    async def handler(reader, writer):
        peers.put_nowait(writer.get_extra_info("peername"))
        await reader.read()
        writer.close()

    target = await asyncio.start_server(handler, "127.0.0.1", 0)
    budget = ResourceBudget(max_connections=1)
    bridge = TCPBridge("127.0.0.1", target.sockets[0].getsockname()[1], budget=budget)
    await bridge.start("127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        peer = await asyncio.wait_for(peers.get(), 1)
        public = bridge._peers[peer][0]
        draining = asyncio.Event()
        original = asyncio.StreamWriter.drain

        async def stalled_drain(stream):
            if stream is public:
                draining.set()
                await asyncio.Event().wait()
            else:
                await original(stream)

        monkeypatch.setattr(asyncio.StreamWriter, "drain", stalled_drain)
        rejection = asyncio.create_task(bridge.reject(peer, b"bounded rejection"))
        await asyncio.wait_for(draining.wait(), 1)
        rejection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rejection
        assert budget.connections.used == 1
        await wait_used(budget.connections, 0)
        assert await asyncio.wait_for(reader.read(), 1) == b"bounded rejection"
        await close_writer(writer)
    finally:
        await bridge.close()
        target.close()
        await target.wait_closed()
    assert budget.connections.used == 0


async def test_admin_state_pages_are_bounded_and_cursor_binds_filters(proxy):
    scenario = rule({"action": "respond", "status": 200}, scope="X-Test-ID")
    async with proxy([scenario], limits={"state_page_size": 2}) as app:
        for scope in ("a", "b", "c", "d", "e"):
            app.engine.decide("orders", "GET", "/fault", {"X-Test-ID": scope}, [])
        async with httpx.AsyncClient(headers={"Authorization": "Bearer test-token"}) as client:
            first = (await client.get(app.admin_url + "/state")).json()
            assert len(first["counters"]) == 2
            cursor = first["next_cursor"]
            mismatch = await client.get(
                app.admin_url + "/state", params={"cursor": cursor, "scope": "a"}
            )
            assert mismatch.status_code == 400
            rows = first["counters"]
            while cursor:
                page = (
                    await client.get(app.admin_url + "/state", params={"cursor": cursor})
                ).json()
                assert len(page["counters"]) <= 2
                rows.extend(page["counters"])
                cursor = page.get("next_cursor")
            assert [row["scope"] for row in rows] == ["a", "b", "c", "d", "e"]
            small = (await client.get(app.admin_url + "/state", params={"scope": "a"})).json()
            assert set(small) == {"counters"}
            assert len(small["counters"]) == 1


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=3", "limit=-1", "limit=1.5", "limit=1&limit=2", "cursor=bad", "typo=1"],
)
async def test_admin_rejects_invalid_paging_without_mutating_state(proxy, query):
    async with proxy(limits={"state_page_size": 2}) as app:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer test-token"}) as client:
            assert (await client.get(app.admin_url + "/state?" + query)).status_code == 400
            assert app.engine.snapshot() == []
