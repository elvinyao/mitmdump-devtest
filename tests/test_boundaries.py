import asyncio
import json
import logging
import socket

import httpx
import pytest
from conftest import rule


async def raw(app, payload):
    reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), 2)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_invalid_method_never_reaches_upstream(proxy, upstream):
    # respond rule ensures that rejection is performed by our proxy, not the upstream parser.
    async with proxy([rule({"action": "respond", "status": 200}, path="/")]) as app:
        response = await raw(app, b"B@D / HTTP/1.1\r\nHost: test\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 400"), response
        assert upstream[1] == []
        assert app.engine.snapshot() == []


async def test_connect_never_opens_arbitrary_target(proxy, upstream):
    async with proxy() as app:
        response = await raw(app, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert response.startswith((b"HTTP/1.1 400", b"HTTP/1.1 405")), response
        assert upstream[1] == []


async def test_scope_not_forwarded_even_when_rule_does_not_match(proxy, upstream):
    async with proxy([rule({"action": "respond", "status": 500}, scope="X-Test-ID")]) as app:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                app.url("orders") + "/normal", headers={"X-Test-ID": "private-test"}
            )
            assert response.status_code == 200
            assert "X-Test-ID" not in upstream[1][0]["headers"]


async def test_original_host_can_be_used_as_match_header(proxy):
    scenario = rule(
        {"action": "respond", "status": 418}, match={"headers": {"Host": "client.example"}}
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        response = await client.get(app.url("orders") + "/", headers={"Host": "client.example"})
        assert response.status_code == 418


async def test_body_limit(proxy, upstream):
    async with proxy(body_limit=8) as app, httpx.AsyncClient() as client:
        response = await client.post(app.url("orders") + "/normal", content=b"123456789")
        assert response.status_code == 413
        assert upstream[1] == []


async def test_admin_request_size_is_bounded(proxy):
    async with proxy() as app, httpx.AsyncClient() as client:
        response = await client.post(
            app.admin_url + "/reset",
            content=b" " * 5000,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 413


async def test_capacity_failure_does_not_become_fake_upstream_503(proxy, upstream):
    scenario = rule({"action": "respond", "status": 500}, scope="X-Test-ID")
    async with proxy([scenario], state={"capacity": 1}) as app, httpx.AsyncClient() as client:
        assert (
            await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": "a"})
        ).status_code == 500
        response = await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": "b"})
        assert response.status_code == 400
        assert response.headers["x-fault-engine-error"] == "scenario"
        assert upstream[1] == []


async def test_events_are_structured_and_redact_request_data(proxy, caplog):
    scenario = rule({"action": "respond", "status": 503}, scope="X-Test-ID")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        with caplog.at_level(logging.INFO, logger="fault_engine"):
            await client.post(
                app.url("orders") + "/fault",
                content=b"SECRET_BODY",
                headers={
                    "X-Test-ID": "x" * 100,
                    "Authorization": "SECRET_TOKEN",
                    "Cookie": "SECRET_COOKIE",
                },
            )
        messages = [json.loads(r.message) for r in caplog.records if r.name == "fault_engine.addon"]
        assert [m["phase"] for m in messages] == ["request", "response"]
        assert all(m["ordinal"] == 1 and m["action"] == "respond" for m in messages)
        assert len(messages[0]["scope"]) == 64
        assert "SECRET" not in caplog.text


async def test_ports_can_be_rebound_after_shutdown(proxy):
    async with proxy() as app:
        public = app.config.services[0].port
        admin = app.config.admin.port
        internal = app.proxyserver.listen_addrs()[0][1]
    for port in [public, admin, internal]:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))


async def test_ipv6_wildcard_advertises_reachable_loopback(proxy, upstream):
    from conftest import free_port

    services = [{"id": "orders", "host": "::", "port": free_port(), "upstream": upstream[0]}]
    async with proxy(services=services, admin={"host": "::", "port": free_port()}) as app:
        assert app.url("orders").startswith("http://[::1]:")
        assert app.admin_url.startswith("http://[::1]:")
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(app.url("orders") + "/")).status_code == 200
            assert (await client.get(app.admin_url + "/health")).status_code == 200


async def test_startup_failure_rolls_back_public_listener(upstream):
    from conftest import free_port

    from fault_engine.config import Config
    from fault_engine.runtime import Runtime

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        public = free_port()
        config = Config.model_validate(
            {
                "services": [{"id": "s", "port": public, "upstream": upstream[0]}],
                "admin": {"port": occupied.getsockname()[1]},
            }
        )
        app = Runtime(config, admin_token="test")
        with pytest.raises(OSError):
            await app.start()
        assert app.master is None
        assert app.proxyserver is None
        assert not app.bridges
        with socket.socket() as rebound:
            rebound.bind(("127.0.0.1", public))
