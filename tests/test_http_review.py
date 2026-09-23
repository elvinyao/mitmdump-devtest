"""Real HTTP wire contracts that are easy to lose through convenience APIs."""

import asyncio
import base64
import gzip
from contextlib import suppress

import pytest
from conftest import free_port, rule


async def response_head(reader):
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    status, *lines = raw[:-4].split(b"\r\n")
    headers = [tuple(line.split(b":", 1)) for line in lines]
    return status, [(name.lower(), value.strip()) for name, value in headers]


async def response_body(reader, headers):
    length = next(int(value) for name, value in headers if name == b"content-length")
    return await asyncio.wait_for(reader.readexactly(length), 2)


@pytest.fixture
async def wire_upstream():
    """A local peer that records custom methods and trailers without normalizing them."""
    calls = []
    writers = set()
    tasks = set()

    async def handler(reader, writer):
        writers.add(writer)
        tasks.add(asyncio.current_task())
        try:
            while True:
                try:
                    raw = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                request_line, *lines = raw[:-4].split(b"\r\n")
                headers = [tuple(line.split(b":", 1)) for line in lines]
                headers = [(name.lower(), value.strip()) for name, value in headers]
                body = b""
                trailers = []
                if (b"transfer-encoding", b"chunked") in headers:
                    while True:
                        length = int((await reader.readline()).split(b";", 1)[0], 16)
                        if not length:
                            while (line := await reader.readline()) != b"\r\n":
                                name, value = line.rstrip(b"\r\n").split(b":", 1)
                                trailers.append((name.lower(), value.strip()))
                            break
                        body += await reader.readexactly(length)
                        assert await reader.readexactly(2) == b"\r\n"
                else:
                    length = next(
                        (int(value) for name, value in headers if name == b"content-length"), 0
                    )
                    body = await reader.readexactly(length)
                calls.append((request_line, headers, body, trailers))
                if b" /trailers " in request_line:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                        b"Trailer: X-Checksum\r\n\r\n3\r\nabc\r\n0\r\nX-Checksum: ok\r\n\r\n"
                    )
                    await writer.drain()
                    continue
                payload = b"upstream"
                encoding = b""
                if b" /compressed " in request_line:
                    payload = gzip.compress(b"compressed upstream \x00\xff", mtime=0)
                    encoding = b"Content-Encoding: gzip\r\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\n"
                    + encoding
                    + b"\r\n"
                )
                if not request_line.startswith(b"HEAD "):
                    writer.write(payload)
                await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("method,expected", [(b"mIxEd", 418), (b"MIXED", 209)])
async def test_method_matching_preserves_case(proxy, method, expected):
    rules = [
        rule({"action": "respond", "status": 418}, match={"methods": ["mIxEd"]}),
        rule({"action": "respond", "status": 209}, id="fallback", match={}),
    ]
    async with proxy(rules) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(method + b" /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            status, _ = await response_head(reader)
            assert int(status.split()[1]) == expected
        finally:
            writer.close()
            await writer.wait_closed()


async def test_mock_preserves_preencoded_binary_body(proxy):
    encoded = gzip.compress(b"binary response \x00\xff", mtime=0)
    scenario = rule(
        {
            "action": "respond",
            "status": 200,
            "body_base64": base64.b64encode(encoded).decode(),
            "headers": {"Content-Encoding": "gzip"},
        }
    )
    async with proxy([scenario]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 200 ")
            assert (b"content-encoding", b"gzip") in headers
            assert await response_body(reader, headers) == encoded
        finally:
            writer.close()
            await writer.wait_closed()


async def test_mock_preserves_latin1_header_bytes(proxy):
    scenario = rule({"action": "respond", "status": 200, "headers": {"X-Label": "caf\u00e9"}})
    async with proxy([scenario]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            _, headers = await response_head(reader)
            assert (b"x-label", b"caf\xe9") in headers
        finally:
            writer.close()
            await writer.wait_closed()


async def test_chunked_upload_preserves_binary_body_and_duplicate_headers(proxy, wire_upstream):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    scenario = rule(
        {"action": "passthrough"},
        scope="X-Test-ID",
        match={"headers": {"X-Label": "first, second"}},
    )
    async with proxy([scenario], services=services) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(
                b"POST /fault HTTP/1.1\r\nHost: test\r\nX-Test-ID: run\r\n"
                b"X-Label: first\r\nx-label: second\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"3;extension=yes\r\nabc\r\n2\r\n\x00\xff\r\n0\r\n\r\n"
            )
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 200 ")
            assert await response_body(reader, headers) == b"upstream"
            assert calls[0][2] == b"abc\x00\xff"
            assert [(name, value) for name, value in calls[0][1] if name == b"x-label"] == [
                (b"x-label", b"first"),
                (b"x-label", b"second"),
            ]
            assert all(name != b"x-test-id" for name, _ in calls[0][1])
            assert app.engine.snapshot()[0]["scope"] == "run"
        finally:
            writer.close()
            await writer.wait_closed()


async def test_options_asterisk_and_custom_method_forwarding(proxy, wire_upstream):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    async with proxy(services=services) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            for line in [b"OPTIONS * HTTP/1.1", b"mIxEd /custom HTTP/1.1"]:
                writer.write(line + b"\r\nHost: arbitrary.example\r\n\r\n")
                await writer.drain()
                status, headers = await response_head(reader)
                assert status.startswith(b"HTTP/1.1 200 ")
                assert await response_body(reader, headers) == b"upstream"
            assert [call[0] for call in calls] == [
                b"OPTIONS * HTTP/1.1",
                b"mIxEd /custom HTTP/1.1",
            ]
        finally:
            writer.close()
            await writer.wait_closed()


async def test_expect_continue_then_binary_upload(proxy, wire_upstream):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    async with proxy(services=services) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(
                b"POST /upload HTTP/1.1\r\nHost: test\r\n"
                b"Expect: 100-continue\r\nContent-Length: 4\r\n\r\n"
            )
            await writer.drain()
            status, _ = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 100 ")
            assert calls == []
            writer.write(b"a\x00\xffz")
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 200 ")
            assert await response_body(reader, headers) == b"upstream"
            assert calls[0][2] == b"a\x00\xffz"
            assert all(name != b"expect" for name, _ in calls[0][1])
        finally:
            writer.close()
            await writer.wait_closed()


async def test_absolute_targets_cannot_override_upstream_on_keepalive(proxy, wire_upstream):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    unexpected_connections = []

    def canary(reader, writer):
        unexpected_connections.append(writer)
        writer.close()

    server = await asyncio.start_server(canary, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with proxy(services=services) as app:
            reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
            try:
                for scheme in ["http", "https"]:
                    target = f"{scheme}://127.0.0.1:{port}/absolute?scheme={scheme}"
                    writer.write(f"GET {target} HTTP/1.1\r\nHost: wrong.example\r\n\r\n".encode())
                    await writer.drain()
                    status, headers = await response_head(reader)
                    assert status.startswith(b"HTTP/1.1 200 ")
                    assert await response_body(reader, headers) == b"upstream"
                assert unexpected_connections == []
                for call, scheme in zip(calls, ["http", "https"], strict=True):
                    target = call[0].split()[1]
                    assert target == f"/absolute?scheme={scheme}".encode()
                    assert (b"host", origin.removeprefix("http://").encode()) in call[1]
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_compressed_upstream_get_and_head_preserve_framing(proxy, wire_upstream):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    encoded = gzip.compress(b"compressed upstream \x00\xff", mtime=0)
    async with proxy(services=services) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            for method in [b"GET", b"HEAD"]:
                writer.write(method + b" /compressed HTTP/1.1\r\nHost: test\r\n\r\n")
                await writer.drain()
                status, headers = await response_head(reader)
                assert status.startswith(b"HTTP/1.1 200 ")
                assert (b"content-encoding", b"gzip") in headers
                assert (b"content-length", str(len(encoded)).encode()) in headers
                if method == b"GET":
                    assert await response_body(reader, headers) == encoded
            writer.write(b"GET /normal HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 200 ")
            assert await response_body(reader, headers) == b"upstream"
            assert len(calls) == 3
        finally:
            writer.close()
            await writer.wait_closed()


async def test_duplicate_scope_headers_rejected_without_counter_collision(proxy, upstream):
    scenario = rule({"action": "respond", "status": 418}, scope="X-Test-ID")
    async with proxy([scenario]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\nX-Test-ID: run, part\r\n\r\n")
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 418 ")
            await response_body(reader, headers)
            writer.write(
                b"GET /fault HTTP/1.1\r\nHost: test\r\nX-Test-ID: run\r\nx-test-id: part\r\n\r\n"
            )
            await writer.drain()
            status, headers = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 400 ")
            assert (b"x-fault-engine-error", b"scenario") in headers
            await response_body(reader, headers)
            assert app.engine.snapshot() == [
                {"service": "orders", "rule": "r", "scope": "run, part", "count": 1}
            ]
            assert upstream[1] == []
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("declared", [True, False])
async def test_request_trailers_fail_explicitly_without_hanging(proxy, upstream, caplog, declared):
    async with proxy() as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            declaration = b"Trailer: X-Checksum\r\n" if declared else b""
            writer.write(
                b"POST /upload HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n"
                + declaration
                + b"\r\n3\r\nabc\r\n0\r\nX-Checksum: ok\r\n\r\n"
            )
            await writer.drain()
            # mitmproxy's request protocol-error path closes before emitting an
            # HTTP response. Unsupported trailers must terminate promptly, never
            # reach the upstream or leave a crashed parser holding the client.
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert upstream[1] == []
            assert "mitmproxy has crashed" not in caplog.text
        finally:
            writer.close()
            await writer.wait_closed()


async def test_upstream_response_trailers_fail_explicitly_without_hanging(
    proxy, wire_upstream, caplog
):
    origin, calls = wire_upstream
    services = [{"id": "orders", "port": free_port(), "upstream": origin}]
    async with proxy(services=services) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(b"GET /trailers HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()
            status, _ = await response_head(reader)
            assert status.startswith(b"HTTP/1.1 502 ")
            assert len(calls) == 1
            assert "mitmproxy has crashed" not in caplog.text
        finally:
            writer.close()
            await writer.wait_closed()
