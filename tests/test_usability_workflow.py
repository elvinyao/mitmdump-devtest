"""Exercise the documented user journey through real CLI processes and HTTP."""

import asyncio
import json
import os
import signal
import sys

import httpx
import pytest
from aiohttp import web
from conftest import free_port


async def invoke(*args, env=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fault_engine",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


@pytest.mark.parametrize("mode", ["http", "html", "redirect", "timeout"])
async def test_admin_cli_remote_errors_are_bounded_and_redacted(mode):
    received = []
    release = asyncio.Event()
    secret = "workflow-private-bearer"

    async def handler(request):
        received.append(request.path)
        if mode == "timeout":
            await release.wait()
        if mode == "redirect":
            return web.Response(status=307, headers={"Location": "/redirect-target"})
        return web.Response(
            status=401 if mode == "http" else 200,
            text=f"<html>{secret} remote-private-details</html>",
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        code, output, error = await invoke(
            "requests",
            "--admin-url",
            f"http://127.0.0.1:{runner.addresses[0][1]}",
            env={**os.environ, "FAULT_ADMIN_TOKEN": secret},
        )
        assert code == 2 and output == "" and error.startswith("error:")
        assert secret not in error and "remote-private-details" not in error
        assert "Traceback" not in error
        assert received == ["/requests"]
        if mode == "timeout":
            assert "timed out" in error
    finally:
        release.set()
        await runner.cleanup()


async def test_admin_cli_refused_port_has_static_error():
    code, output, error = await invoke(
        "requests",
        "--admin-url",
        f"http://127.0.0.1:{free_port()}",
        env={**os.environ, "FAULT_ADMIN_TOKEN": "workflow-private-bearer"},
    )
    assert code == 2 and output == "" and error.startswith("error:")
    assert "workflow-private-bearer" not in error and "Traceback" not in error


async def test_cli_retry_workflow_and_isolated_rerun(tmp_path, upstream):
    config_path = str(tmp_path / "retry.yaml")
    port, admin_port = free_port(), free_port()
    env = {**os.environ, "FAULT_ADMIN_TOKEN": "workflow-token"}
    local_env = dict(env)
    local_env.pop("FAULT_ADMIN_TOKEN")
    code, _, error = await invoke(
        "init",
        config_path,
        "--upstream",
        upstream[0],
        "--preset",
        "retry",
        "--service",
        "orders",
        "--path",
        "/retry",
        "--port",
        str(port),
        "--admin-port",
        str(admin_port),
        env=local_env,
    )
    assert code == 0, error
    code, output, error = await invoke("validate", config_path, env=local_env)
    assert code == 0 and "valid" in output, error
    code, output, error = await invoke(
        "explain",
        config_path,
        "--service",
        "orders",
        "--path",
        "/retry?private=secret-query",
        "--header",
        "X-Test-Run-ID: run-a",
        "--header",
        "Authorization: secret-auth",
        "--ordinal",
        "3",
        env=local_env,
    )
    assert code == 0, error
    explanation = json.loads(output)
    assert explanation["matched_rule"] == "retry"
    assert explanation["decision"]["action"] == "passthrough"
    assert "secret" not in output
    assert upstream[1] == []

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fault_engine",
        "serve",
        config_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    admin_url = f"http://127.0.0.1:{admin_port}"

    async def admin(command, *args, expected=0, token_env=None):
        code, output, error = await invoke(
            command,
            "--admin-url",
            admin_url,
            *args,
            env=env if token_env is None else token_env,
        )
        assert code == expected, (output, error)
        if expected == 2:
            assert output == "" and error
            assert "workflow-token" not in error
            assert "Traceback" not in error
            return None
        assert error == ""
        return json.loads(output)

    selection = ("--service", "orders", "--rule", "retry", "--scope", "run-a")
    try:
        assert process.stdout is not None and process.stderr is not None
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (await process.stderr.read()).decode()
        assert json.loads(line)["event"] == "ready"
        initial = await admin("journal-clear")
        start = initial["checkpoint"]
        await admin("requests", expected=2, token_env=local_env)
        await admin("requests", expected=2, token_env={**env, "FAULT_ADMIN_TOKEN": "wrong"})
        await admin("verify", "--count", "-1", expected=2)

        async with httpx.AsyncClient(trust_env=False) as client:

            async def attempt(scope):
                return await client.get(
                    f"http://127.0.0.1:{port}/retry?private=secret-query",
                    headers={
                        "X-Test-Run-ID": scope,
                        "Authorization": "secret-auth",
                        "Cookie": "secret-cookie",
                    },
                )

            async def retry_round():
                # This is the caller's explicit test retry policy, not client-library behavior.
                statuses = []
                for _ in range(3):
                    statuses.append((await attempt("run-a")).status_code)
                assert statuses == [503, 503, 200]

            await retry_round()
            assert len(upstream[1]) == 1
            page = await admin("requests", *selection, "--after", start, "--limit", "2")
            assert page["complete"] is True
            assert [row["status"] for row in page["requests"]] == [503, 503]
            next_page = await admin(
                "requests",
                *selection,
                "--after",
                page["next_cursor"],
                "--limit",
                "2",
            )
            assert [row["status"] for row in next_page["requests"]] == [200]
            assert next_page["requests"][0]["upstream_received"] is True
            assert "secret" not in json.dumps([page, next_page])
            assert "/retry" not in json.dumps([page, next_page])
            checked = await admin(
                "verify",
                *selection,
                "--after",
                start,
                "--count",
                "3",
                "--statuses",
                "503",
                "503",
                "200",
                "--min-interval-seconds",
                "0",
            )
            assert checked["matched"] is True and checked["complete"] is True
            for options, failure in (
                (("--count", "4"), "count"),
                (("--count", "3", "--statuses", "503", "200", "200"), "statuses"),
                (("--count", "3", "--min-interval-seconds", "999"), "min_interval_seconds"),
            ):
                checked = await admin("verify", *selection, "--after", start, *options, expected=1)
                assert checked["matched"] is False and failure in checked["failures"]

            assert (await attempt("run-b")).status_code == 503
            assert len(upstream[1]) == 1
            await admin("verify", *selection, "--count", "3")
            await admin("verify", "--scope", "run-b", "--count", "1", "--statuses", "503")
            assert (await admin("reset", *selection))["reset"] == 1
            # Counter reset preserves all observations; clearing them preserves run-b's ordinal.
            await admin("verify", *selection, "--count", "3")
            cleared = await admin("journal-clear")
            checkpoint = cleared["checkpoint"]
            assert checkpoint != start
            incomplete = await admin(
                "verify", *selection, "--after", start, "--count", "0", expected=1
            )
            assert incomplete["matched"] is False and incomplete["complete"] is False
            empty = await admin("requests", "--after", checkpoint)
            assert empty["requests"] == [] and empty["complete"] is True
            assert (await attempt("run-b")).status_code == 503
            assert (await attempt("run-b")).status_code == 200
            await retry_round()
            assert len(upstream[1]) == 3
            await admin(
                "verify",
                *selection,
                "--after",
                checkpoint,
                "--count",
                "3",
                "--statuses",
                "503",
                "503",
                "200",
            )
            await admin(
                "verify",
                "--scope",
                "run-b",
                "--after",
                checkpoint,
                "--count",
                "2",
                "--statuses",
                "503",
                "200",
            )
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
