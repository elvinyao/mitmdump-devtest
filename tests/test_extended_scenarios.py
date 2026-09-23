import asyncio
import errno
import socket
import time

import httpx
import pytest
from conftest import free_port, rule
from pydantic import ValidationError

from fault_engine.config import Config


@pytest.mark.parametrize("action", ["respond", "respond_after"])
async def test_delayed_synthetic_response_phase(proxy, upstream, action):
    scenario = rule(
        {"action": action, "status": 503, "json_body": {"retry": True}, "delay_seconds": 0.1}
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        started = time.monotonic()
        response = await client.post(app.url("orders") + "/fault", content=b"write")
        assert response.status_code == 503
        assert response.json() == {"retry": True}
        assert time.monotonic() - started >= 0.09
        assert len(upstream[1]) == (1 if action == "respond_after" else 0)


async def test_respond_after_retry_hits_backend_twice(proxy, upstream):
    scenario = rule({"action": "respond_after", "status": 503, "body": "lost acknowledgement"})
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        responses = [
            await client.post(app.url("orders") + "/fault", content=b"payment=1") for _ in range(2)
        ]
        assert [r.status_code for r in responses] == [503, 200]
        assert responses[0].text == "lost acknowledgement"
        assert "x-upstream" not in responses[0].headers
        assert len(upstream[1]) == 2
        assert all(c["body"] == "cGF5bWVudD0x" for c in upstream[1])


async def test_reset_after_is_real_rst_after_backend_write(proxy, upstream):
    async with proxy([rule({"action": "reset_after"})]) as app:

        def send():
            with socket.create_connection(("127.0.0.1", app.config.services[0].port), 2) as sock:
                sock.sendall(b"POST /fault HTTP/1.1\r\nHost: test\r\nContent-Length: 3\r\n\r\nabc")
                with pytest.raises(ConnectionResetError) as error:
                    sock.recv(4096)
                assert error.value.errno == errno.ECONNRESET

        await asyncio.to_thread(send)
        assert len(upstream[1]) == 1
        assert upstream[1][0]["body"] == "YWJj"


async def test_disconnect_after_backend_write(proxy, upstream):
    async with proxy([rule({"action": "disconnect_after"})]) as app, httpx.AsyncClient() as client:
        with pytest.raises(httpx.TransportError):
            await client.post(app.url("orders") + "/fault", content=b"write")
        assert len(upstream[1]) == 1
        assert (await client.get(app.url("orders") + "/normal")).status_code == 200


@pytest.mark.parametrize("action", ["respond", "respond_after"])
async def test_delayed_mock_client_timeout_and_other_client_liveness(proxy, upstream, action):
    scenario = rule({"action": action, "status": 200, "body": "ok", "delay_seconds": 0.5})
    async with proxy([scenario]) as app:
        async with httpx.AsyncClient(timeout=0.1) as slow, httpx.AsyncClient() as fast:
            task = asyncio.create_task(slow.get(app.url("orders") + "/fault"))
            async with asyncio.timeout(2):
                while not app.addon.pending:
                    await asyncio.sleep(0.001)
            assert (await fast.get(app.url("orders") + "/normal")).status_code == 200
            with pytest.raises(httpx.ReadTimeout):
                await task
            assert len([c for c in upstream[1] if c["path"] == "/fault"]) == (
                1 if action == "respond_after" else 0
            )


async def test_respond_after_head_does_not_corrupt_next_response(proxy):
    scenario = rule({"action": "respond_after", "status": 200, "body": "replacement"})
    async with proxy([scenario]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"HEAD /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert b"content-length: 11\r\n" in first.lower()
            writer.write(b"GET /normal HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            await writer.drain()
            second = await asyncio.wait_for(reader.read(), 2)
            assert second.startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("action", ["respond_after", "reset_after", "disconnect_after"])
async def test_after_actions_preserve_real_upstream_failure(proxy, action):
    step = {"action": action}
    if action == "respond_after":
        step.update(status=200, body="must not replace transport failure")
    # A bound but non-listening socket guarantees the selected upstream refuses
    # connections, without relying on a conventionally unused system port.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        services = [
            {
                "id": "orders",
                "port": free_port(),
                "upstream": f"http://127.0.0.1:{unused.getsockname()[1]}",
            }
        ]
        async with proxy([rule(step)], services=services) as app, httpx.AsyncClient() as client:
            response = await client.post(app.url("orders") + "/fault", content=b"write")
            assert response.status_code == 502
            assert "must not replace" not in response.text
            assert app.engine.snapshot()[0]["count"] == 1


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), 3601, "1", True])
def test_invalid_mock_delays(value):
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "services": [{"id": "s", "port": 8080, "upstream": "http://localhost"}],
                "rules": [
                    {
                        "id": "r",
                        "service": "s",
                        "sequence": [
                            {"action": "respond_after", "status": 200, "delay_seconds": value}
                        ],
                    }
                ],
            }
        )


@pytest.mark.parametrize("action", ["reset", "reset_after"])
async def test_reset_failure_head_preserves_keepalive_framing(proxy, action, monkeypatch):
    async with proxy([rule({"action": action})]) as app:
        monkeypatch.setattr(app.bridges["orders"], "reset", lambda peer: False)
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"HEAD /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert first.startswith(b"HTTP/1.1 503")
            writer.write(b"GET /normal HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            await writer.drain()
            second = await asyncio.wait_for(reader.read(), 2)
            assert second.startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()
