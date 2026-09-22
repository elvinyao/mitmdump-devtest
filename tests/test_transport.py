import asyncio
import errno
import importlib.util
import socket
from contextlib import asynccontextmanager, suppress

import pytest


@asynccontextmanager
async def setup_bridge(handler):
    assert importlib.util.find_spec("fault_engine.transport") is not None
    from fault_engine.transport import TCPBridge

    tasks = set()
    writers = []

    def connected(reader, writer):
        writers.append(writer)
        task = asyncio.create_task(handler(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    target = await asyncio.start_server(connected, "127.0.0.1", 0)
    bridge = TCPBridge("127.0.0.1", target.sockets[0].getsockname()[1])
    await bridge.start("127.0.0.1", 0)
    try:
        async with asyncio.timeout(5):
            yield bridge
    finally:
        await bridge.close()
        target.close()
        for writer in writers:
            writer.transport.abort()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await target.wait_closed()


async def echo(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def test_binary_bidirectional_relay():
    async with setup_bridge(echo) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        payload = bytes(range(256)) * 1000
        writer.write(payload)
        await writer.drain()
        assert await reader.readexactly(len(payload)) == payload
        writer.close()
        await writer.wait_closed()


async def test_client_half_close_preserves_response():
    async def after_eof(reader, writer):
        request = await reader.read()
        writer.write(b"response:" + request)
        await writer.drain()
        writer.close()

    async with setup_bridge(after_eof) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(b"request")
        writer.write_eof()
        assert await reader.read() == b"response:request"
        writer.close()
        await writer.wait_closed()


async def test_target_half_close_preserves_client_upload_and_removes_stale_peer():
    peers = asyncio.Queue()
    received = asyncio.Queue()

    async def half_close(reader, writer):
        peers.put_nowait(writer.get_extra_info("peername"))
        writer.write(b"greeting")
        writer.write_eof()
        received.put_nowait(await reader.read())
        writer.close()

    async with setup_bridge(half_close) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        peer = await peers.get()
        assert await reader.read() == b"greeting"
        assert bridge.owns(peer)
        writer.write(b"upload after target EOF")
        writer.write_eof()
        assert await received.get() == b"upload after target EOF"
        writer.close()
        await writer.wait_closed()
        await asyncio.gather(*bridge._tasks)
        assert bridge.owns(peer) is False
        assert bridge.reset(peer) is False


async def test_close_cleans_connections_during_accept():
    async with setup_bridge(echo) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        await bridge.close()
        assert await reader.read() == b""
        assert not bridge._tasks
        assert not bridge._clients
        writer.close()
        await writer.wait_closed()


async def test_reset_is_linux_econnreset_and_other_client_survives():
    peers = asyncio.Queue()

    async def record_echo(reader, writer):
        peers.put_nowait(writer.get_extra_info("peername"))
        await echo(reader, writer)

    async with setup_bridge(record_echo) as bridge:
        loop = asyncio.get_running_loop()
        with socket.socket() as victim:
            victim.setblocking(False)
            await loop.sock_connect(victim, ("127.0.0.1", bridge.port))
            peer = await peers.get()
            await loop.sock_sendall(victim, b"ready")
            assert await loop.sock_recv(victim, 5) == b"ready"
            assert bridge.owns(peer) is True
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
            await peers.get()
            assert bridge.reset(peer) is True
            with pytest.raises(ConnectionResetError) as error:
                await loop.sock_recv(victim, 1)
            assert error.value.errno == errno.ECONNRESET == 104
            assert bridge.reset(peer) is False
            assert bridge.owns(peer) is False
            assert bridge.reset(("127.0.0.1", 1)) is False
            writer.write(b"alive")
            await writer.drain()
            assert await reader.readexactly(5) == b"alive"
            writer.close()
            await writer.wait_closed()


async def test_close_cleans_active_connections_and_listener():
    async with setup_bridge(echo) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        port = bridge.port
        writer.write(b"ready")
        await writer.drain()
        assert await reader.readexactly(5) == b"ready"
        await bridge.close()
        assert await reader.read() == b""
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", port)
        assert not bridge._tasks
        writer.close()
        await writer.wait_closed()


async def test_unavailable_target_closes_client():
    assert importlib.util.find_spec("fault_engine.transport") is not None
    from fault_engine.transport import TCPBridge

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        bridge = TCPBridge("127.0.0.1", reserved.getsockname()[1])
        await bridge.start("127.0.0.1", 0)
        try:
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
                assert await reader.read() == b""
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
        finally:
            await bridge.close()


async def test_close_does_not_wait_for_blocked_receiver():
    accepted = asyncio.Event()

    async def never_read(reader, writer):
        writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        accepted.set()
        await asyncio.Event().wait()

    async with setup_bridge(never_read) as bridge:
        _, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        await accepted.wait()
        writer.write(b"x" * (32 * 1024 * 1024))
        # Let real TCP send/receive buffers fill; this receiver never drains them.
        await asyncio.sleep(0.1)
        assert writer.transport.get_write_buffer_size() > 0
        try:
            await asyncio.wait_for(bridge.close(), 1)
            assert not bridge._tasks
            assert not bridge._clients
        finally:
            writer.transport.abort()
            with suppress(OSError):
                await writer.wait_closed()


async def test_failed_reset_preserves_connection_ownership(monkeypatch):
    peers = asyncio.Queue()

    async def record_echo(reader, writer):
        peers.put_nowait(writer.get_extra_info("peername"))
        await echo(reader, writer)

    async with setup_bridge(record_echo) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        peer = await peers.get()
        writer.write(b"ready")
        await writer.drain()
        assert await reader.readexactly(5) == b"ready"
        public_socket = bridge._peers[peer][0].get_extra_info("socket")
        original = type(public_socket).setsockopt

        def fail_linger(sock, level, option, value):
            if sock is public_socket and option == socket.SO_LINGER:
                raise OSError(errno.ENOPROTOOPT, "injected socket option failure")
            return original(sock, level, option, value)

        # Only inject the rare OS setsockopt error; all relay I/O stays real.
        with monkeypatch.context() as patch:
            patch.setattr(type(public_socket), "setsockopt", fail_linger)
            assert bridge.reset(peer) is False
        assert bridge.owns(peer)
        writer.write(b"still alive")
        await writer.drain()
        assert await reader.readexactly(11) == b"still alive"
        writer.close()
        await writer.wait_closed()
