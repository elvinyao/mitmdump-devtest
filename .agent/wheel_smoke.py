"""Exercise the installed wheel and its private HTTP compatibility boundary."""

import asyncio
import json
import socket
import subprocess
import sys
from contextlib import ExitStack, suppress
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

import fault_engine
from fault_engine.config import Config
from fault_engine.runtime import Runtime


def check_installation() -> None:
    assert fault_engine.__file__ is not None
    assert Path(fault_engine.__file__).is_relative_to(sys.prefix), fault_engine.__file__
    schema_result = subprocess.run(
        [sys.executable, "-m", "fault_engine", "schema"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(schema_result.stdout)["type"] == "object"


async def main() -> None:
    calls: list[bytes] = []
    tasks: set[asyncio.Task] = set()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            calls.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Trailer: X-Checksum\r\nConnection: close\r\n\r\n"
                b"3\r\nabc\r\n0\r\nX-Checksum: ok\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(upstream(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    # Keep both reservations open until construction to avoid selecting the same port twice.
    with ExitStack() as reservations:
        ports = []
        for _ in range(2):
            sock = reservations.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
        config = Config.model_validate(
            {
                "services": [
                    {
                        "id": "wheel",
                        "port": ports[0],
                        "upstream": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    }
                ],
                "admin": {"port": ports[1]},
                "rules": [
                    {
                        "id": "mock",
                        "service": "wheel",
                        "match": {"path": "/mock"},
                        "sequence": [{"action": "respond", "status": 201, "body": "wheel"}],
                    }
                ],
            }
        )
    runtime = Runtime(config, admin_token="wheel-test")
    try:
        await runtime.start()
        async with ClientSession(timeout=ClientTimeout(total=5)) as client:
            async with client.get(runtime.url("wheel") + "/mock") as response:
                assert response.status == 201
                assert await response.text() == "wheel"
            assert calls == []
            reader, writer = await asyncio.open_connection("127.0.0.1", ports[0])
            try:
                writer.write(
                    b"POST /upload HTTP/1.1\r\nHost: wheel\r\nTransfer-Encoding: chunked\r\n"
                    b"Trailer: X-Checksum\r\n\r\n3\r\nabc\r\n0\r\nX-Checksum: ok\r\n\r\n"
                )
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 3) == b""
                assert calls == []
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
            async with client.get(runtime.url("wheel") + "/trailers") as response:
                assert response.status == 502
                await response.read()
            assert len(calls) == 1
    finally:
        await runtime.close()
        server.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print("Installed wheel: schema, HTTP response, request/response trailer checks passed")


if __name__ == "__main__":
    check_installation()
    asyncio.run(main())
