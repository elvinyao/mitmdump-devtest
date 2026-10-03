"""Exercise a response-driven client, rather than a fixed number of HTTP calls."""

import asyncio
import json
import sys

import httpx
import pytest
from conftest import rule


async def client_run(url, *options):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "examples/retry_client.py",
        url,
        "--run-id",
        "client-test",
        "--retry-delay",
        "0",
        *options,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert not error, error.decode()
    return process.returncode, json.loads(output)


@pytest.mark.parametrize("status", [429, 503])
async def test_client_retries_only_until_backend_success(proxy, upstream, status):
    scenario = rule({"action": "respond", "status": status, "repeat": 2}, scope="X-Test-Run-ID")
    async with proxy([scenario]) as app:
        code, result = await client_run(app.url("orders") + "/fault")
        assert code == 0 and result["outcome"] == "success"
        assert [attempt["status"] for attempt in result["attempts"]] == [status, status, 200]
        assert len(upstream[1]) == 1
        assert app.journal.verify(scope="client-test", count=3, statuses=[status, status, 200])[
            "matched"
        ]


async def test_client_stops_on_nonretryable_error(proxy, upstream):
    scenario = rule(
        {"action": "respond", "status": 401}, scope="X-Test-Run-ID", after_sequence="repeat_last"
    )
    async with proxy([scenario]) as app:
        code, result = await client_run(app.url("orders") + "/fault")
        assert code == 1 and result["outcome"] == "http_error"
        assert [attempt["status"] for attempt in result["attempts"]] == [401]
        assert app.journal.verify(count=1, statuses=[401])["matched"]
        assert upstream[1] == []


async def test_client_stops_when_retry_budget_is_exhausted(proxy, upstream):
    scenario = rule(
        {"action": "respond", "status": 503}, scope="X-Test-Run-ID", after_sequence="repeat_last"
    )
    async with proxy([scenario]) as app:
        code, result = await client_run(app.url("orders") + "/fault")
        assert code == 1 and result["outcome"] == "retry_exhausted"
        assert [attempt["status"] for attempt in result["attempts"]] == [503] * 3
        assert app.journal.verify(count=3, statuses=[503] * 3)["matched"]
        assert upstream[1] == []


async def test_client_reports_invalid_encoding_without_retrying_a_200(proxy, upstream):
    scenario = rule(
        {
            "action": "respond",
            "status": 200,
            "headers": {"Content-Encoding": "gzip"},
            "body": "not gzip",
        },
        scope="X-Test-Run-ID",
        after_sequence="repeat_last",
    )
    async with proxy([scenario]) as app:
        code, result = await client_run(app.url("orders") + "/fault")
        assert code == 1 and result["outcome"] == "decode_error"
        assert len(result["attempts"]) == 1
        assert app.journal.verify(count=1, statuses=[200])["matched"]
        assert upstream[1] == []


async def test_client_read_timeout_then_fixed_success(proxy, upstream):
    scenario = rule(
        {"action": "timeout", "seconds": 2.0},
        scope="X-Test-Run-ID",
        sequence=[{"action": "timeout", "seconds": 2.0}, {"action": "respond", "status": 200}],
        after_sequence="repeat_last",
    )
    async with proxy([scenario]) as app:
        code, result = await client_run(app.url("orders") + "/fault", "--timeout", "0.2")
        assert code == 0 and result["outcome"] == "success"
        assert [attempt["outcome"] for attempt in result["attempts"]] == ["read_timeout", "http"]
        assert [attempt["status"] for attempt in result["attempts"]] == [None, 200]
        async with asyncio.timeout(2):
            while not app.journal.verify(count=2)["complete"]:
                await asyncio.sleep(0.005)
        assert app.journal.verify(count=2)["matched"]
        assert upstream[1] == []


async def test_nth_two_4xx_then_fixed_ok_with_real_client(proxy, upstream):
    scenario = rule(
        {"action": "respond", "status": 429, "repeat": 2},
        scope="X-Test-Run-ID",
        start_at=5,
        sequence=[
            {"action": "respond", "status": 429, "repeat": 2},
            {"action": "respond", "status": 200},
        ],
        after_sequence="repeat_last",
    )
    async with proxy([scenario]) as app:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(4):
                response = await client.get(
                    app.url("orders") + "/fault", headers={"X-Test-Run-ID": "client-test"}
                )
                assert response.status_code == 200
        code, result = await client_run(app.url("orders") + "/fault")
        assert code == 0 and result["outcome"] == "success"
        assert [attempt["status"] for attempt in result["attempts"]] == [429, 429, 200]
        assert app.journal.verify(count=7, statuses=[200] * 4 + [429, 429, 200])["matched"]
        assert len(upstream[1]) == 4
