import base64
import socket
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from fault_engine.config import Config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def upstream():
    calls = []

    async def handler(request):
        body = await request.read()
        record = {
            "method": request.method,
            "path": request.raw_path,
            "headers": dict(request.headers),
            "body": base64.b64encode(body).decode(),
        }
        calls.append(record)
        return web.json_response(record, headers={"X-Upstream": "yes"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        await runner.cleanup()


@pytest.fixture
def proxy(upstream):
    @asynccontextmanager
    async def factory(rules=None, **overrides):
        from fault_engine.runtime import Runtime

        data = {
            "services": [{"id": "orders", "port": free_port(), "upstream": upstream[0]}],
            "admin": {"port": free_port()},
            "rules": rules or [],
        }
        data.update(overrides)
        config = Config.model_validate(data)
        runtime = Runtime(config, admin_token="test-token")
        await runtime.start()
        try:
            yield runtime
        finally:
            await runtime.close()

    return factory


def rule(action, *, path="/fault", **overrides):
    result = {"id": "r", "service": "orders", "match": {"path": path}, "sequence": [action]}
    result.update(overrides)
    return result
