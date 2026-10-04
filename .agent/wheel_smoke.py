"""Exercise the installed wheel and its private HTTP compatibility boundary."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

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
    with TemporaryDirectory() as directory:
        config_path = str(Path(directory) / "retry.yaml")
        for args in (
            [
                "init",
                config_path,
                "--upstream",
                "http://127.0.0.1:9",
                "--preset",
                "retry",
                "--service",
                "wheel",
            ],
            ["validate", config_path],
        ):
            subprocess.run(
                [sys.executable, "-m", "fault_engine", *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        explained = subprocess.run(
            [
                sys.executable,
                "-m",
                "fault_engine",
                "explain",
                config_path,
                "--service",
                "wheel",
                "--path",
                "/retry",
                "--header",
                "X-Test-Run-ID: wheel-run",
                "--ordinal",
                "3",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        result = json.loads(explained.stdout)
        assert result["matched_rule"] == "retry"
        assert result["decision"]["action"] == "passthrough"


async def admin_command(runtime: Runtime, command: str, *args: str, expected: int = 0) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fault_engine",
        command,
        "--admin-url",
        runtime.admin_url,
        *args,
        env={**os.environ, "FAULT_ADMIN_TOKEN": "wheel-test"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == expected, error.decode()
    assert error == b"", error.decode()
    return json.loads(output)


async def main() -> None:
    calls: list[bytes] = []
    tasks: set[asyncio.Task] = set()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            calls.append(await reader.readuntil(b"\r\n\r\n"))
            if calls[-1].startswith(b"hEaD /custom "):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n"
                )
            elif calls[-1].startswith(b"GET /interim "):
                writer.write(
                    b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
                    b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc"
                )
            else:
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
            async with client.post(
                runtime.admin_url + "/verify",
                json={"service": "wheel", "rule": "mock", "count": 1, "statuses": [201]},
                headers={"Authorization": "Bearer wheel-test"},
            ) as response:
                assert response.status == 200
                verified = await response.json()
                assert verified["matched"] is True and verified["complete"] is True
            observed = await admin_command(runtime, "requests", "--service", "wheel")
            assert [row["status"] for row in observed["requests"]] == [201]
            verified = await admin_command(
                runtime,
                "verify",
                "--rule",
                "mock",
                "--count",
                "1",
                "--statuses",
                "201",
            )
            assert verified["matched"] is True and verified["complete"] is True
            failed = await admin_command(
                runtime,
                "verify",
                "--rule",
                "mock",
                "--count",
                "2",
                expected=1,
            )
            assert failed["matched"] is False and failed["complete"] is True
            async with client.post(
                runtime.admin_url + "/reset?rule=missing",
                json={},
                headers={"Authorization": "Bearer wheel-test"},
            ) as response:
                assert response.status == 400
                await response.read()
            assert (await admin_command(runtime, "reset", "--rule", "mock"))["reset"] == 1
            checkpoint = (await admin_command(runtime, "journal-clear"))["checkpoint"]
            await admin_command(runtime, "verify", "--after", checkpoint, "--count", "0")
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
            reader, writer = await asyncio.open_connection("127.0.0.1", ports[0])
            try:
                writer.write(b"hEaD /custom HTTP/1.1\r\nHost: wheel\r\nConnection: close\r\n\r\n")
                await writer.drain()
                wire = await asyncio.wait_for(reader.read(), 3)
                headers, body = wire.split(b"\r\n\r\n", 1)
                assert headers.startswith(b"HTTP/1.1 200")
                assert b"content-length: 3" in headers.lower()
                assert b"transfer-encoding" not in headers.lower()
                assert body == b"abc"
                assert len(calls) == 2 and calls[-1].startswith(b"hEaD /custom ")
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
            checkpoint = runtime.journal.checkpoint
            async with client.get(runtime.url("wheel") + "/interim") as response:
                assert response.status == 200
                assert await response.read() == b"abc"
            assert len(calls) == 3
            assert runtime.journal.verify(after=checkpoint, count=1, statuses=[200])["matched"]
    finally:
        await runtime.close()
        server.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print(
        "Installed wheel: schema, init/validate/explain, admin requests/verify/reset/clear, "
        "HTTP response, custom method framing, interim response, reset query rejection, "
        "request/response trailer checks passed"
    )


if __name__ == "__main__":
    check_installation()
    asyncio.run(main())
