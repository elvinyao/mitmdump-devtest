import argparse
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from conftest import free_port

from fault_engine import admin_client


def arguments(*values):
    parser = argparse.ArgumentParser()
    admin_client.register_admin_commands(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(values)


@pytest.fixture
def admin_server():
    @asynccontextmanager
    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            yield f"http://127.0.0.1:{runner.addresses[0][1]}"
        finally:
            await runner.cleanup()

    return start


@pytest.mark.parametrize(
    ("command", "flags", "method", "path", "payload", "result"),
    [
        (
            "requests",
            ("--after", "instance:4", "--limit", "7"),
            "GET",
            "/requests",
            {
                "service": "orders",
                "rule": "retry",
                "scope": "run-1",
                "after": "instance:4",
                "limit": "7",
            },
            {"requests": [], "checkpoint": "instance:4", "complete": True},
        ),
        (
            "verify",
            (
                "--after",
                "instance:4",
                "--count",
                "3",
                "--statuses",
                "503",
                "503",
                "200",
                "--min-interval-seconds",
                "0.1",
            ),
            "POST",
            "/verify",
            {
                "service": "orders",
                "rule": "retry",
                "scope": "run-1",
                "after": "instance:4",
                "count": 3,
                "statuses": [503, 503, 200],
                "min_interval_seconds": 0.1,
            },
            {"matched": True, "complete": True},
        ),
        (
            "reset",
            (),
            "POST",
            "/reset",
            {"service": "orders", "rule": "retry", "scope": "run-1"},
            {"reset": 2},
        ),
    ],
)
async def test_selected_commands_send_expected_request(
    admin_server, monkeypatch, capsys, command, flags, method, path, payload, result
):
    received = []

    async def handler(request):
        received.append(
            (
                request.method,
                request.path,
                dict(request.query) if request.method == "GET" else await request.json(),
                request.headers.get("Authorization"),
            )
        )
        return web.json_response(result)

    monkeypatch.setenv("CUSTOM_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments(
            command,
            "--admin-url",
            url,
            "--token-env",
            "CUSTOM_ADMIN_TOKEN",
            "--service",
            "orders",
            "--rule",
            "retry",
            "--scope",
            "run-1",
            *flags,
        )
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 0
    assert received == [(method, path, payload, "Bearer secret-bearer")]
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert output.err == ""


async def test_journal_clear_sends_empty_object(admin_server, monkeypatch, capsys):
    received = []

    async def handler(request):
        received.append((request.method, request.path, await request.json()))
        return web.json_response({"checkpoint": "instance:8"})

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("journal-clear", "--admin-url", url)
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 0
    assert received == [("POST", "/requests/reset", {})]
    assert json.loads(capsys.readouterr().out) == {"checkpoint": "instance:8"}


def test_command_defaults_and_token_flag_absence():
    args = arguments("requests")
    assert args.admin_url == "http://127.0.0.1:9090"
    assert args.token_env == "FAULT_ADMIN_TOKEN"
    assert args.limit == 100
    with pytest.raises(SystemExit) as exc:
        arguments("requests", "--token", "secret")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        arguments("verify")
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "matched,complete,code",
    [(True, True, 0), (False, True, 1), (False, False, 1), (True, False, 1)],
)
async def test_verify_exit_status(admin_server, monkeypatch, capsys, matched, complete, code):
    async def handler(request):
        return web.json_response({"matched": matched, "complete": complete})

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("verify", "--admin-url", url, "--count", "0")
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == code
    output = capsys.readouterr()
    assert json.loads(output.out) == {"matched": matched, "complete": complete}
    assert output.err == ""


@pytest.mark.parametrize(
    "result",
    [
        {"matched": "true", "complete": True},
        {"matched": 1, "complete": True},
        {"matched": True, "complete": "true"},
        {"matched": True},
        {"complete": True},
        {"error": "secret-bearer"},
        ["secret-bearer"],
        None,
    ],
)
async def test_invalid_verify_response_cannot_pass(admin_server, monkeypatch, capsys, result):
    async def handler(request):
        return web.json_response(result)

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("verify", "--admin-url", url, "--count", "0")
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "invalid response" in output.err
    assert "secret-bearer" not in output.err


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "has space",
        "secret\nvalue",
        "secret\rvalue",
        "secret\tvalue",
        "secret\x7f",
        "秘密",
    ],
)
def test_missing_or_invalid_token_does_not_make_request(monkeypatch, capsys, token):
    monkeypatch.delenv("FAULT_ADMIN_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("FAULT_ADMIN_TOKEN", token)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not start an HTTP session")

    monkeypatch.setattr(admin_client.aiohttp, "ClientSession", forbidden)
    assert admin_client.run_admin_command(arguments("requests")) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "admin token" in output.err
    if token:
        assert token not in output.err


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/admin",
        "ftp://example.com",
        "http://",
        "http://host:0",
        "http://host:65536",
        "http://user:secret@host",
        "http://host/path",
        "http://host/?",
        "http://host/#",
        "http://host?secret",
        "http://host#secret",
        "http://host\n",
        "http://[bad-ipv6]",
    ],
)
def test_invalid_origin_fails_before_session_and_token_lookup(monkeypatch, capsys, url):
    args = arguments("requests", "--admin-url", url)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid URL must fail before token lookup or HTTP session")

    monkeypatch.setattr(admin_client.os.environ, "get", forbidden)
    monkeypatch.setattr(admin_client.aiohttp, "ClientSession", forbidden)
    assert admin_client.run_admin_command(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "admin URL" in output.err
    assert "secret" not in output.err


@pytest.mark.parametrize(
    "values",
    [
        ("requests", "--limit", "0"),
        ("requests", "--limit", "1001"),
        ("verify", "--count", "-1"),
        ("verify", "--count", "1", "--statuses", "99"),
        ("verify", "--count", "1", "--statuses", "600"),
        ("verify", "--count", "2", "--statuses", "200"),
        ("verify", "--count", "1", "--min-interval-seconds", "nan"),
        ("verify", "--count", "1", "--min-interval-seconds", "inf"),
        ("verify", "--count", "1", "--min-interval-seconds", "-0.1"),
        ("requests", "--token-env", "BAD=NAME"),
    ],
)
def test_invalid_request_options_do_not_make_request(monkeypatch, capsys, values):
    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid input must not start an HTTP session")

    monkeypatch.setattr(admin_client.aiohttp, "ClientSession", forbidden)
    assert admin_client.run_admin_command(arguments(*values)) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err


@pytest.mark.parametrize("status", [401, 400, 500])
async def test_http_errors_never_echo_remote_body(admin_server, monkeypatch, capsys, status):
    async def handler(request):
        return web.Response(status=status, text="<script>secret-bearer remote-error</script>")

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("requests", "--admin-url", url)
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err
    assert "secret-bearer" not in output.err
    assert "remote-error" not in output.err


async def test_redirect_is_not_followed(admin_server, monkeypatch, capsys):
    received = []

    async def target(request):
        received.append(request.headers.get("Authorization"))
        return web.json_response({"reset": 0})

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(target) as target_url:

        async def redirect(request):
            return web.Response(status=307, headers={"Location": target_url + "/leak"})

        async with admin_server(redirect) as url:
            args = arguments("reset", "--admin-url", url)
            assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    assert received == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "body",
    [
        "<html>secret-bearer</html>",
        '{"matched": true, "complete": true, "x": NaN}',
        '{"matched": false, "matched": true, "complete": true}',
    ],
)
async def test_malformed_response_is_not_echoed(admin_server, monkeypatch, capsys, body):
    async def handler(request):
        return web.Response(text=body)

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("verify", "--admin-url", url, "--count", "0")
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "invalid response" in output.err
    assert "secret-bearer" not in output.err


async def test_deeply_nested_response_is_rejected_before_printing(
    admin_server, monkeypatch, capsys
):
    body = (
        '{"matched":true,"complete":true,"extra":'
        + "[" * 1200
        + '"secret-bearer"'
        + "]" * 1200
        + "}"
    )

    async def handler(request):
        return web.Response(text=body, content_type="application/json")

    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        args = arguments("verify", "--admin-url", url, "--count", "0")
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "error: admin server returned an invalid response\n"


@pytest.mark.parametrize(
    "command,limit_name",
    [("requests", "MAX_RESPONSE_BYTES"), ("verify", "MAX_VERIFY_RESPONSE_BYTES")],
)
async def test_oversized_response_is_rejected(
    admin_server, monkeypatch, capsys, command, limit_name
):
    monkeypatch.setattr(admin_client, limit_name, 64)
    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")

    async def handler(request):
        return web.json_response(
            {
                "matched": True,
                "complete": True,
                "requests": [],
                "checkpoint": "instance:1",
                "junk": "x" * 100,
            }
        )

    async with admin_server(handler) as url:
        flags = ("--count", "0") if command == "verify" else ()
        args = arguments(command, "--admin-url", url, *flags)
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "exceeded the size limit" in output.err


async def test_timeout_is_finite_and_static(admin_server, monkeypatch, capsys):
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return web.json_response({"reset": 0})

    monkeypatch.setattr(admin_client, "REQUEST_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    async with admin_server(handler) as url:
        try:
            args = arguments("reset", "--admin-url", url)
            async with asyncio.timeout(2):
                assert await asyncio.to_thread(admin_client.run_admin_command, args) == 2
        finally:
            release.set()
    output = capsys.readouterr()
    assert output.out == ""
    assert "timed out" in output.err
    assert "secret-bearer" not in output.err


def test_refused_port_is_static(monkeypatch, capsys):
    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    args = arguments("reset", "--admin-url", f"http://127.0.0.1:{free_port()}")
    assert admin_client.run_admin_command(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "connect" in output.err
    assert "secret-bearer" not in output.err


async def test_environment_proxy_is_ignored(admin_server, monkeypatch, capsys):
    sessions = []
    real_session = admin_client.aiohttp.ClientSession

    def session(*args, **kwargs):
        sessions.append(kwargs)
        return real_session(*args, **kwargs)

    async def handler(request):
        return web.json_response({"reset": 0})

    monkeypatch.setattr(admin_client.aiohttp, "ClientSession", session)
    monkeypatch.setenv("FAULT_ADMIN_TOKEN", "secret-bearer")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("NO_PROXY", "")
    async with admin_server(handler) as url:
        args = arguments("reset", "--admin-url", url)
        assert await asyncio.to_thread(admin_client.run_admin_command, args) == 0
    assert sessions[0]["trust_env"] is False
    assert sessions[0]["timeout"].total == 5
    assert json.loads(capsys.readouterr().out) == {"reset": 0}
