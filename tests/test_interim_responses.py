"""Informational upstream responses must not finish buffered fault decisions."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

import pytest
from conftest import free_port, rule

EARLY_HINTS = b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
FINAL = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nupstream"


@dataclass
class InterimPeer:
    port: int = 0
    prelude: tuple[bytes, ...] = (EARLY_HINTS,)
    final: bytes = FINAL
    close_without_final: bool = False
    calls: list[tuple[bytes, bytes]] = field(default_factory=list)
    sent: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
async def interim_peer():
    peer = InterimPeer()
    peer.release.set()
    tasks = set()
    writers = set()

    async def handler(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    (
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    ),
                    0,
                )
                body = await reader.readexactly(length)
                peer.calls.append((head, body))
                if b" /next " in head:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nnext")
                else:
                    for part in peer.prelude:
                        writer.write(part)
                        await writer.drain()
                        await asyncio.sleep(0)
                    peer.sent.set()
                    await peer.release.wait()
                    if peer.close_without_final:
                        return
                    writer.write(peer.final)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    peer.port = server.sockets[0].getsockname()[1]
    try:
        yield peer
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


def services(peer):
    return [{"id": "orders", "port": free_port(), "upstream": f"http://127.0.0.1:{peer.port}"}]


async def read_head(reader):
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    first, *lines = raw[:-4].split(b"\r\n")
    return int(first.split()[1]), {
        name.lower(): value.strip() for name, value in (line.split(b":", 1) for line in lines)
    }


async def read_final(reader):
    status, headers = await read_head(reader)
    body = await asyncio.wait_for(reader.readexactly(int(headers[b"content-length"])), 2)
    return status, body


async def connect(app):
    reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
    writer.write(b"GET /fault HTTP/1.1\r\nHost: local\r\n\r\n")
    await writer.drain()
    return reader, writer


@pytest.mark.parametrize(
    "action,expected",
    [
        ({"action": "passthrough"}, (200, b"upstream")),
        ({"action": "respond_after", "status": 503, "body": "fault"}, (503, b"fault")),
        ({"action": "delay_after", "seconds": 0.01}, (200, b"upstream")),
    ],
)
@pytest.mark.parametrize("partial_body", [False, True])
async def test_interim_does_not_finish_response_or_post_upstream_action(
    proxy, interim_peer, action, expected, partial_body
):
    if partial_body:
        interim_peer.prelude = (EARLY_HINTS + FINAL[:-3],)
        interim_peer.final = FINAL[-3:]
    interim_peer.release.clear()
    async with proxy([rule(action)], services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            await asyncio.wait_for(interim_peer.sent.wait(), 2)
            response = asyncio.create_task(read_final(reader))
            done, _ = await asyncio.wait({response}, timeout=0.05)
            assert not done, "an informational response must not become the final response"
            row = app.journal.page()["requests"][0]
            assert row["outcome"] == "pending"
            assert row["status"] is None
            assert row["upstream_received"] is False
            assert app.budget.requests.used == 1
            interim_peer.release.set()
            assert await response == expected
            row = app.journal.page()["requests"][0]
            assert row["outcome"] == "response_prepared"
            assert row["status"] == expected[0]
            assert row["upstream_received"] is True
            assert app.budget.requests.used == 0
            writer.write(b"GET /next HTTP/1.1\r\nHost: local\r\n\r\n")
            await writer.drain()
            assert await read_final(reader) == (200, b"next")
            assert len(interim_peer.calls) == 2
        finally:
            interim_peer.release.set()
            if "response" in locals():
                response.cancel()
                await asyncio.gather(response, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.mark.parametrize("fragmented", [False, True])
async def test_multiple_interims_and_final_in_same_buffer_or_fragments(
    proxy, interim_peer, fragmented
):
    wire = (
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 102 Processing\r\n\r\n" + EARLY_HINTS + EARLY_HINTS + FINAL
    )
    interim_peer.prelude = (
        tuple(wire[i : i + 7] for i in range(0, len(wire), 7)) if fragmented else (wire,)
    )
    interim_peer.final = b""
    async with proxy(services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            assert await read_final(reader) == (200, b"upstream")
            rows = app.journal.page()["requests"]
            assert len(rows) == 1
            assert rows[0]["status"] == 200
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.mark.parametrize("action", ["passthrough", "respond_after", "reset_after"])
@pytest.mark.parametrize("incomplete_final", [b"", FINAL[:-3], b"HTTP/1.1 invalid\r\n\r\n"])
async def test_interim_then_eof_is_upstream_error_not_success(
    proxy, interim_peer, action, incomplete_final
):
    interim_peer.prelude = (EARLY_HINTS + incomplete_final,)
    interim_peer.close_without_final = True
    step = {"action": action}
    if action == "respond_after":
        step.update(status=201, body="must not happen")
    async with proxy([rule(step)], services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            assert (await read_final(reader))[0] == 502
            row = app.journal.page()["requests"][0]
            assert row["outcome"] == "transport_error"
            assert row["status"] is None
            assert row["upstream_received"] is False
            assert app.budget.requests.used == 0
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.mark.parametrize("action", ["disconnect_after", "reset_after"])
async def test_post_upstream_disconnect_waits_for_final_body(proxy, interim_peer, action):
    interim_peer.prelude = (EARLY_HINTS + FINAL[:-3],)
    interim_peer.final = FINAL[-3:]
    interim_peer.release.clear()
    async with proxy([rule({"action": action})], services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        response = asyncio.create_task(reader.read())
        try:
            await asyncio.wait_for(interim_peer.sent.wait(), 2)
            done, _ = await asyncio.wait({response}, timeout=0.05)
            assert not done
            row = app.journal.page()["requests"][0]
            assert row["outcome"] == "pending"
            assert row["upstream_received"] is False
            interim_peer.release.set()
            if action == "reset_after":
                with pytest.raises(ConnectionResetError) as error:
                    await asyncio.wait_for(response, 2)
                assert error.value.errno == 104
            else:
                assert await asyncio.wait_for(response, 2) == b""
            row = app.journal.page()["requests"][0]
            assert row["outcome"] == action
            assert row["status"] is None
            assert row["upstream_received"] is True
        finally:
            interim_peer.release.set()
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


async def test_expect_continue_upload_survives_upstream_interims(proxy, interim_peer):
    interim_peer.prelude = (b"HTTP/1.1 100 Continue\r\n\r\n" + EARLY_HINTS,)
    async with proxy(services=services(interim_peer)) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        try:
            writer.write(
                b"POST /fault HTTP/1.1\r\nHost: local\r\n"
                b"Expect: 100-continue\r\nContent-Length: 6\r\n\r\n"
            )
            await writer.drain()
            assert (await read_head(reader))[0] == 100
            writer.write(b"upload")
            await writer.drain()
            assert await read_final(reader) == (200, b"upstream")
            assert len(interim_peer.calls) == 1
            assert interim_peer.calls[0][1] == b"upload"
            assert b"expect:" not in interim_peer.calls[0][0].lower()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


async def test_101_is_not_waited_on_as_an_informational_prelude(proxy, interim_peer):
    # Preserve mitmproxy's existing upgrade hand-off; this is not a promise to
    # support upgraded protocols in Fault Engine.
    interim_peer.final = (
        b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: test\r\n\r\n"
    )
    async with proxy(services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            assert (await read_head(reader))[0] == 101
            row = app.journal.page()["requests"][0]
            assert row["status"] == 101
            assert row["upstream_received"] is True
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.mark.parametrize("count", [100, 101])
async def test_interim_limit_is_per_request_and_fails_as_upstream_error(proxy, interim_peer, count):
    interim_peer.prelude = (EARLY_HINTS * count,)
    async with proxy(services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            if count == 101:
                assert (await read_final(reader))[0] == 502
                row = app.journal.page()["requests"][0]
                assert row["outcome"] == "transport_error"
                assert row["status"] is None
                assert row["upstream_received"] is False
            else:
                assert await read_final(reader) == (200, b"upstream")
                writer.write(b"GET /fault HTTP/1.1\r\nHost: local\r\n\r\n")
                await writer.drain()
                assert await read_final(reader) == (200, b"upstream")
                assert len(interim_peer.calls) == 2
                assert [row["status"] for row in app.journal.page()["requests"]] == [200, 200]
            assert app.budget.requests.used == 0
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


@pytest.mark.parametrize("close_runtime", [False, True])
async def test_cancel_between_interim_and_final_releases_resources(
    proxy, interim_peer, close_runtime
):
    interim_peer.release.clear()
    async with proxy(services=services(interim_peer)) as app:
        reader, writer = await connect(app)
        try:
            await asyncio.wait_for(interim_peer.sent.wait(), 2)
            if close_runtime:
                await asyncio.wait_for(app.close(), 2)
            else:
                writer.close()
                await writer.wait_closed()
            for _ in range(100):
                if app.budget.requests.used == 0:
                    break
                await asyncio.sleep(0.01)
            assert app.budget.requests.used == 0
            row = app.journal.page()["requests"][0]
            assert row["outcome"] in {"client_disconnected", "shutdown", "transport_error"}
            assert row["upstream_received"] is False
            assert row["status"] is None
        finally:
            interim_peer.release.set()
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
