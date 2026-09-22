"""Two local demo backends plus the real proxy, all inside the Docker runner."""

import asyncio
import base64
import logging
from pathlib import Path

from aiohttp import web

from fault_engine.cli import serve
from fault_engine.config import load_config


async def main() -> None:
    runners: list[web.AppRunner] = []
    config = load_config(Path(__file__).with_name("scenarios.yaml"))
    try:
        for name, port in (("orders", 9000), ("inventory", 9001)):
            calls = [0]

            async def echo(
                request: web.Request, service: str = name, counter: list[int] = calls
            ) -> web.Response:
                counter[0] += 1
                body = await request.read()
                return web.json_response(
                    {
                        "service": service,
                        "upstream_calls": counter[0],
                        "method": request.method,
                        "path": request.raw_path,
                        "body_base64": base64.b64encode(body).decode(),
                    }
                )

            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", echo)
            runner = web.AppRunner(app, access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
        await serve(config, "local-demo-token")
    finally:
        for runner in runners:
            await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("fault_engine").setLevel(logging.INFO)
    asyncio.run(main())
