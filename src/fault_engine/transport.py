"""Opaque TCP relay with targeted resets of the public client socket."""

import asyncio
import socket
import struct
from contextlib import suppress


class TCPBridge:
    def __init__(self, target_host: str, target_port: int):
        self.target_host = target_host
        self.target_port = target_port
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._clients: set[asyncio.StreamWriter] = set()
        self._peers: dict[tuple, tuple[asyncio.StreamWriter, asyncio.Task[None]]] = {}

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("TCP bridge is not listening")
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str, port: int) -> None:
        if self._server is not None:
            raise RuntimeError("TCP bridge is already started")
        self._server = await asyncio.start_server(self._accept, host, port)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        task = asyncio.create_task(self._connect(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def owns(self, peer: tuple) -> bool:
        connection = self._peers.get(peer)
        return connection is not None and not connection[0].is_closing()

    def reset(self, peer: tuple) -> bool:
        connection = self._peers.get(peer)
        if connection is None:
            return False
        writer, task = connection
        if writer.is_closing():
            self._peers.pop(peer, None)
            return False
        public_socket = writer.get_extra_info("socket")
        try:
            public_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            return False
        self._peers.pop(peer, None)
        writer.transport.abort()
        task.cancel()
        return True

    @staticmethod
    async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
            await writer.drain()

    async def _connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        internal_writer = None
        peer = None
        completed = False
        relays: list[asyncio.Task[None]] = []
        try:
            internal_reader, internal_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
            peer = internal_writer.get_extra_info("sockname")
            task = asyncio.current_task()
            assert task is not None
            self._peers[peer] = (writer, task)
            relays = [
                asyncio.create_task(self._relay(reader, internal_writer)),
                asyncio.create_task(self._relay(internal_reader, writer)),
            ]
            await asyncio.gather(*relays)
            completed = True
        except (OSError, ConnectionError):
            pass
        finally:
            if peer is not None:
                self._peers.pop(peer, None)
            for relay in relays:
                relay.cancel()
            writers = [writer] if internal_writer is None else [writer, internal_writer]
            for stream in writers:
                if completed:
                    stream.close()
                else:
                    # Cancellation and failed relays must not flush into a blocked peer.
                    stream.transport.abort()
            try:
                await asyncio.gather(*relays, return_exceptions=True)
                for stream in writers:
                    with suppress(OSError):
                        await stream.wait_closed()
            finally:
                # Also cover cancellation arriving during graceful cleanup itself.
                for stream in writers:
                    stream.transport.abort()
                self._clients.discard(writer)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Accepted tasks may be cancelled before their coroutine enters its finally block.
        for writer in self._clients:
            writer.transport.abort()
        for writer in self._clients:
            with suppress(OSError):
                await writer.wait_closed()
        self._clients.clear()
        self._tasks.clear()
        self._peers.clear()
        if server is not None:
            await server.wait_closed()
