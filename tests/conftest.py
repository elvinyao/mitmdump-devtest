import base64
import socket
from contextlib import ExitStack, asynccontextmanager

import pytest
from aiohttp import web

from fault_engine.config import Config

_allocated_ports: set[int] = set()


def free_port():
    # Several tests choose service/admin ports before binding either listener.
    # Keep rejected candidates reserved and never hand out a prior test's port again.
    # This prevents duplicate selections; it cannot reserve ports against other processes.
    with ExitStack() as reservations:
        for _ in range(128):
            sock = reservations.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            if port not in _allocated_ports:
                _allocated_ports.add(port)
                return port
    raise RuntimeError("could not allocate a distinct test port")


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
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
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
