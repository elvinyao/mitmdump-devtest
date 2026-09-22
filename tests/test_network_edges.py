"""Network boundary contracts over real HTTP/1.1 sockets and TLS upstreams."""

import asyncio
import base64
import ipaddress
import socket
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from aiohttp import web
from conftest import free_port, rule
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
async def tls_upstream(tmp_path):
    """Generate a private CA and a separately signed server certificate in Docker."""
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ephemeral test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = [tmp_path / name for name in ("ca.pem", "cert.pem", "key.pem")]
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    calls = []

    async def handler(request):
        calls.append(
            {"path": request.raw_path, "tls": request.transport.get_extra_info("ssl_object")}
        )
        return web.Response(body=b"secure upstream", headers={"X-TLS-Upstream": "yes"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    site = web.TCPSite(runner, "127.0.0.1", port, ssl_context=context)
    await site.start()
    try:
        yield f"https://127.0.0.1:{port}", ca_path, calls
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("trusted", [True, False])
async def test_https_upstream_certificate_verification(proxy, tls_upstream, trusted):
    origin, ca_path, calls = tls_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    options = {"upstream_ca": str(ca_path)} if trusted else {}
    async with proxy(services=services, **options) as app, httpx.AsyncClient() as client:
        assert app.master.options.ssl_insecure is False
        response = await client.get(app.url("orders") + "/secure?a=1")
        if trusted:
            assert response.status_code == 200, response.text
            assert response.content == b"secure upstream"
            assert response.headers["x-tls-upstream"] == "yes"
            assert len(calls) == 1
            assert calls[0]["path"] == "/secure?a=1"
            assert calls[0]["tls"] is not None
        else:
            assert response.status_code == 502
            assert calls == []


async def test_distinct_service_targets_and_independent_rule_state(proxy, upstream):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(text="inventory")

    target = web.Application()
    target.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(target)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    services = [
        {"id": "orders", "port": free_port(), "upstream": upstream[0]},
        {"id": "inventory", "port": free_port(), "upstream": f"http://127.0.0.1:{port}"},
    ]
    rules = [
        rule(
            {"action": "respond", "status": status}, id=service, service=service, scope="X-Test-ID"
        )
        for service, status in [("orders", 429), ("inventory", 503)]
    ]
    try:
        async with proxy(rules, services=services) as app, httpx.AsyncClient() as client:
            headers = {"X-Test-ID": "shared"}
            assert (
                await client.get(app.url("orders") + "/fault", headers=headers)
            ).status_code == 429
            assert (
                await client.get(app.url("orders") + "/fault", headers=headers)
            ).status_code == 200
            assert (
                await client.get(app.url("inventory") + "/fault", headers=headers)
            ).status_code == 503
            response = await client.get(app.url("inventory") + "/fault", headers=headers)
            assert response.status_code == 200
            assert response.text == "inventory"
            assert [c["path"] for c in upstream[1]] == ["/fault"]
            assert calls == ["/fault"]
            assert {(c["service"], c["count"]) for c in app.engine.snapshot()} == {
                ("orders", 2),
                ("inventory", 2),
            }
    finally:
        await runner.cleanup()


async def test_unavailable_upstream_returns_502(proxy):
    # Reserve without listening: nobody else can claim this intentionally unavailable port.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        services = [{"id": "orders", "port": free_port(), "upstream": f"http://127.0.0.1:{port}"}]
        async with proxy(services=services) as app, httpx.AsyncClient() as client:
            response = await client.get(app.url("orders") + "/unavailable")
            assert response.status_code == 502


@pytest.mark.parametrize("missing_scope", [False, True])
async def test_mock_head_has_no_body_and_preserves_same_connection(proxy, upstream, missing_scope):
    scenario = rule(
        {"action": "respond", "status": 200, "body": "mock body"},
        scope="X-Test-ID" if missing_scope else "global",
    )
    async with proxy([scenario]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"HEAD /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert head.startswith(b"HTTP/1.1 400 " if missing_scope else b"HTTP/1.1 200 ")
            if not missing_scope:
                assert b"content-length: 9\r\n" in head.lower()
            writer.write(b"GET /normal HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            # Any forbidden HEAD body bytes would precede this status line.
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert response.startswith(b"HTTP/1.1 200 ")
            lengths = [
                line.split(b":", 1)[1]
                for line in response.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ]
            await asyncio.wait_for(reader.readexactly(int(lengths[0])), 2)
            assert [c["path"] for c in upstream[1]] == ["/normal"]
        finally:
            writer.close()
            await writer.wait_closed()


async def test_binary_mock_body_is_exact(proxy, upstream):
    body = bytes(range(256)) * 4
    scenario = rule(
        {
            "action": "respond",
            "status": 200,
            "body_base64": base64.b64encode(body).decode(),
            "headers": {"Content-Type": "application/octet-stream"},
        }
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        response = await client.get(app.url("orders") + "/fault")
        assert response.status_code == 200
        assert response.content == body
        assert int(response.headers["content-length"]) == len(body)
        assert upstream[1] == []


@pytest.mark.parametrize("action", ["delay_before", "timeout"])
async def test_client_cancellation_consumes_step_and_later_requests_survive(
    proxy, upstream, action
):
    async with proxy([rule({"action": action, "seconds": 0.25})]) as app:
        async with httpx.AsyncClient() as abandoned, httpx.AsyncClient() as client:
            task = asyncio.create_task(abandoned.get(app.url("orders") + "/fault?cancelled=1"))
            async with asyncio.timeout(2):
                while not app.addon.pending:
                    await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            response = await client.get(app.url("orders") + "/fault?later=1")
            assert response.status_code == 200
            assert app.engine.snapshot()[0]["count"] == 2
            async with asyncio.timeout(2):
                while app.addon.pending:
                    await asyncio.sleep(0.005)
            assert [c["path"] for c in upstream[1]] == ["/fault?later=1"]
            assert (await client.get(app.url("orders") + "/normal")).status_code == 200


async def test_admin_reset_does_not_change_inflight_decision(proxy, upstream):
    scenario = rule(
        {"action": "delay_before", "seconds": 0.15},
        sequence=[
            {"action": "delay_before", "seconds": 0.15},
            {"action": "respond", "status": 503},
        ],
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        original = asyncio.create_task(client.get(app.url("orders") + "/fault?original=1"))
        async with asyncio.timeout(2):
            while not app.addon.pending:
                await asyncio.sleep(0.005)
        reset = await client.post(
            app.admin_url + "/reset",
            json={"rule": "r"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert reset.status_code == 200
        assert reset.json() == {"reset": 1}
        assert app.engine.snapshot() == []
        assert (await original).status_code == 200
        assert app.engine.snapshot() == []
        assert (await client.get(app.url("orders") + "/fault?new=1")).status_code == 200
        assert (await client.get(app.url("orders") + "/fault?new=2")).status_code == 503
        assert [c["path"] for c in upstream[1]] == ["/fault?original=1", "/fault?new=1"]
