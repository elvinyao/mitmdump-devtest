import asyncio
import base64
import errno
import socket
import time

import httpx
import pytest
from conftest import free_port, rule


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
)
async def test_methods_body_query_and_headers(proxy, upstream, method):
    async with proxy() as app, httpx.AsyncClient() as client:
        response = await client.request(
            method,
            app.url("orders") + "/echo?a=1&a=2",
            content=b"\x00\xffhello",
            headers={"X-Business": "value"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["x-upstream"] == "yes"
        record = upstream[1][-1]
        assert record["method"] == method
        assert record["path"] == "/echo?a=1&a=2"
        assert base64.b64decode(record["body"]) == b"\x00\xffhello"
        assert record["headers"]["X-Business"] == "value"
        assert record["headers"]["Host"] == upstream[0].removeprefix("http://")
        if method == "HEAD":
            assert response.content == b""


async def test_custom_method_passthrough(proxy):
    # aiohttp's llhttp parser deliberately rejects unknown verbs; use a raw test upstream.
    received = []

    async def handle(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        received.append(await reader.readexactly(3))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        services = [{"id": "orders", "port": free_port(), "upstream": f"http://127.0.0.1:{port}"}]
        async with proxy(services=services) as app, httpx.AsyncClient() as client:
            response = await client.request("CUSTOM", app.url("orders") + "/echo", content=b"abc")
            assert response.status_code == 200
            assert received[0].startswith(b"CUSTOM /echo HTTP/1.1\r\n")
            assert received[1] == b"abc"


async def test_real_retry_two_errors_then_upstream_once(proxy, upstream):
    scenario = rule(
        {
            "action": "respond",
            "status": 429,
            "repeat": 2,
            "headers": {"Retry-After": "0"},
            "json_body": {"error": "retry"},
        },
        scope="X-Test-ID",
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        statuses = []
        for _ in range(3):
            response = await client.post(
                app.url("orders") + "/fault", content=b"payment", headers={"X-Test-ID": "same-call"}
            )
            statuses.append(response.status_code)
            if response.status_code != 429:
                break
            assert response.json() == {"error": "retry"}
            assert response.headers["retry-after"] == "0"
        assert statuses == [429, 429, 200]
        assert len(upstream[1]) == 1
        assert "X-Test-ID" not in upstream[1][0]["headers"]
        assert app.engine.snapshot()[0]["count"] == 3


@pytest.mark.parametrize(
    "status", [200, 204, 205, 304, 400, 401, 404, 408, 429, 500, 502, 503, 504]
)
async def test_mock_statuses(proxy, upstream, status):
    async with (
        proxy([rule({"action": "respond", "status": status})]) as app,
        httpx.AsyncClient() as client,
    ):
        response = await client.get(app.url("orders") + "/fault")
        assert response.status_code == status
        assert response.content == b""
        assert upstream[1] == []


@pytest.mark.parametrize(
    "action,expected_calls", [("delay_before", 0), ("delay_after", 1), ("timeout", 0)]
)
async def test_timeout_phase_and_no_head_of_line_blocking(proxy, upstream, action, expected_calls):
    async with proxy([rule({"action": action, "seconds": 0.6})]) as app:
        async with httpx.AsyncClient(timeout=0.2) as slow, httpx.AsyncClient() as fast:
            task = asyncio.create_task(slow.post(app.url("orders") + "/fault", content=b"write"))
            # Observe rule assignment rather than guessing when the slow request started.
            async with asyncio.timeout(2):
                while not app.engine.snapshot():
                    await asyncio.sleep(0.005)
            start = time.monotonic()
            assert (await fast.get(app.url("orders") + "/normal")).status_code == 200
            assert time.monotonic() - start < 0.4
            with pytest.raises(httpx.ReadTimeout):
                await task
            fault_calls = [c for c in upstream[1] if c["path"] == "/fault"]
            assert len(fault_calls) == expected_calls


@pytest.mark.parametrize("action", ["delay_before", "delay_after"])
async def test_delay_eventually_succeeds(proxy, upstream, action):
    async with (
        proxy([rule({"action": action, "seconds": 0.1})]) as app,
        httpx.AsyncClient() as client,
    ):
        start = time.monotonic()
        assert (await client.get(app.url("orders") + "/fault")).status_code == 200
        assert time.monotonic() - start >= 0.09
        assert len(upstream[1]) == 1


async def test_real_reset_and_unrelated_connection_survives(proxy, upstream):
    async with proxy([rule({"action": "reset"})]) as app, httpx.AsyncClient() as client:
        assert (await client.get(app.url("orders") + "/normal")).status_code == 200

        def raw_request():
            with socket.create_connection(
                ("127.0.0.1", app.config.services[0].port), timeout=2
            ) as sock:
                sock.sendall(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
                with pytest.raises(ConnectionResetError) as error:
                    sock.recv(1024)
                assert error.value.errno == errno.ECONNRESET == 104

        await asyncio.to_thread(raw_request)
        assert (await client.get(app.url("orders") + "/normal")).status_code == 200
        assert [c["path"] for c in upstream[1]] == ["/normal", "/normal"]


@pytest.mark.parametrize("action", ["disconnect", "reset", "timeout"])
async def test_transport_fault_is_not_http_response(proxy, upstream, action):
    step = {"action": action}
    if action == "timeout":
        step["seconds"] = 0.05
    async with proxy([rule(step)]) as app, httpx.AsyncClient(timeout=2) as client:
        with pytest.raises(httpx.TransportError):
            await client.get(app.url("orders") + "/fault")
        assert upstream[1] == []


async def test_admin_auth_and_filtered_reset(proxy):
    scenario = rule({"action": "respond", "status": 503}, scope="X-Test-ID")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        assert (await client.get(app.admin_url + "/health")).status_code == 200
        for scope in ["a", "b"]:
            await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": scope})
        assert (await client.post(app.admin_url + "/reset", json={})).status_code == 401
        auth = {"Authorization": "Bearer test-token"}
        assert (await client.get(app.admin_url + "/state")).status_code == 401
        state = (await client.get(app.admin_url + "/state", headers=auth)).json()
        assert len(state["counters"]) == 2
        response = await client.post(app.admin_url + "/reset", headers=auth, json={"scope": "a"})
        assert response.json() == {"reset": 1}
        assert app.engine.snapshot()[0]["scope"] == "b"
        assert (
            await client.post(app.admin_url + "/reset", headers=auth, json={"typo": 1})
        ).status_code == 400
        assert (
            await client.post(app.admin_url + "/reset", headers=auth, content="not json")
        ).status_code == 400
        assert (await client.get(app.admin_url + "/rules", headers=auth)).json()["rules"][0][
            "id"
        ] == "r"


async def test_missing_scope_explicit_error(proxy, upstream):
    async with (
        proxy([rule({"action": "reset"}, scope="X-Test-ID")]) as app,
        httpx.AsyncClient() as client,
    ):
        response = await client.get(app.url("orders") + "/fault")
        assert response.status_code == 400
        assert response.headers["x-fault-engine-error"] == "scenario"
        assert "scope" in response.json()["error"]
        assert upstream[1] == []


async def test_shutdown_closes_ports_with_inflight_delay(proxy):
    async with proxy([rule({"action": "timeout", "seconds": 60.0})]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        async with asyncio.timeout(2):
            while not app.engine.snapshot():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(app.close(), 2)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
