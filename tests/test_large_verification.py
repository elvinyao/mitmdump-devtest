"""The largest supported journal must remain verifiable through the bounded CLI."""

import argparse
import asyncio
import itertools
import json
import os
import sys
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from fault_engine import admin_client
from fault_engine.admin import make_admin
from fault_engine.config import Config
from fault_engine.engine import Engine
from fault_engine.journal import Journal


@asynccontextmanager
async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


async def invoke_verify(url, count):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fault_engine",
        "verify",
        "--admin-url",
        url,
        "--count",
        str(count),
        "--min-interval-seconds",
        "0",
        env={**os.environ, "FAULT_ADMIN_TOKEN": "large-journal-token"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert not error, error.decode()
    return process.returncode, json.loads(output)


async def test_maximum_journal_cli_verifies_complete_large_results():
    config = Config.model_validate(
        {
            "services": [{"id": "backend", "port": 8080, "upstream": "http://127.0.0.1:9"}],
            "limits": {"journal_capacity": 100_000},
        }
    )
    # Small, non-integer monotonic intervals exercise full float serialization.
    clock = itertools.count(0.0, 0.0001234567890123456)
    journal = Journal(config.limits.journal_capacity, clock=clock.__next__)
    for ordinal in range(1, 100_001):
        request_id = str(ordinal)
        journal.start(
            request_id,
            service="backend",
            rule="retry",
            scope="large-run",
            method="GET",
            ordinal=ordinal,
            action="respond",
            sampled=True,
        )
        journal.finish(request_id, "response_prepared", status=200)
    assert len(json.dumps(journal.verify(count=100_000)).encode()) > 2 * 1024 * 1024
    async with serve(
        make_admin(config, Engine(config), "large-journal-token", journal=journal)
    ) as url:
        for count, expected_code in [(100_000, 0), (99_999, 1)]:
            code, result = await invoke_verify(url, count)
            assert code == expected_code
            assert result["complete"] is True
            assert result["matched"] is (expected_code == 0)
            assert result["actual"]["count"] == 100_000
            assert result["actual"]["statuses"] == [200] * 100_000
            assert len(result["actual"]["intervals_seconds"]) == 99_999
            assert result["failures"] == ([] if expected_code == 0 else ["count"])


@pytest.mark.parametrize("command", ["requests", "verify"])
@pytest.mark.parametrize("extra_byte", [0, 1])
async def test_command_response_limits_remain_bounded_with_chunked_body(command, extra_byte):
    limit = (4 if command == "verify" else 2) * 1024 * 1024
    result = (
        {"matched": True, "complete": True}
        if command == "verify"
        else {"requests": [], "checkpoint": "checkpoint", "complete": True}
    )
    result["padding"] = ""
    result["padding"] = "x" * (limit + extra_byte - len(json.dumps(result).encode()))
    body = json.dumps(result).encode()
    assert len(body) == limit + extra_byte

    async def handler(request):
        response = web.Response(body=body, content_type="application/json")
        response.enable_chunked_encoding()
        return response

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    async with serve(app) as url:
        args = argparse.Namespace(command=command)
        options: tuple[str, str, dict[str, str], dict[str, object]] = (
            ("POST", "/verify", {}, {"count": 0})
            if command == "verify"
            else ("GET", "/requests", {}, {})
        )
        if extra_byte:
            with pytest.raises(admin_client.AdminClientError, match="exceeded the size limit"):
                await admin_client._request(args, url, "test-token", options)
        else:
            assert await admin_client._request(args, url, "test-token", options) == result
