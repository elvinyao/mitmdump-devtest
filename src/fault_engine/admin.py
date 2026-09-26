"""Separate authenticated control plane; never routed through scenario matching."""

import hmac
import json
from collections.abc import Awaitable, Callable

from aiohttp import web
from pydantic import ValidationError

from fault_engine.config import Config, Model
from fault_engine.engine import Engine


class CounterSelection(Model):
    service: str | None = None
    rule: str | None = None
    scope: str | None = None


def make_admin(config: Config, engine: Engine, token: str) -> web.Application:
    @web.middleware
    async def auth(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.path != "/health" and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
        ):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def state(request: web.Request) -> web.Response:
        try:
            if len(request.query) != len(set(request.query)):
                raise ValueError("duplicate query parameter")
            selection = CounterSelection.model_validate(dict(request.query))
        except ValueError:
            return web.json_response(
                {"error": "expected optional service, rule, scope query parameters, once each"},
                status=400,
            )
        return web.json_response(
            {
                "counters": engine.snapshot(
                    service=selection.service, rule=selection.rule, scope=selection.scope
                )
            }
        )

    async def rules(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "rules": [
                    {
                        "id": r.id,
                        "service": r.service,
                        "scope": r.scope,
                        "start_at": r.start_at,
                        "after_sequence": r.after_sequence,
                        "actions": [a.action for a in r.sequence],
                        "match": {
                            "methods": r.match.methods,
                            "path": r.match.path,
                            "path_regex": r.match.path_regex,
                            "header_names": list(r.match.headers),
                            "query_names": list(r.match.query),
                        },
                        # An allowlist keeps response bodies and header/query values
                        # private even when future action models gain fields.
                        "sequence": [
                            a.model_dump(
                                include={"action", "repeat", "status", "seconds", "delay_seconds"}
                            )
                            for a in r.sequence
                        ],
                    }
                    for r in config.rules
                ]
            }
        )

    async def reset(request: web.Request) -> web.Response:
        try:
            selection = CounterSelection.model_validate(await request.json())
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return web.json_response(
                {"error": "expected object with optional service, rule, scope strings"}, status=400
            )
        return web.json_response(
            {
                "reset": engine.reset(
                    service=selection.service, rule=selection.rule, scope=selection.scope
                )
            }
        )

    app = web.Application(middlewares=[auth], client_max_size=4096)
    app.router.add_get("/health", health)
    app.router.add_get("/state", state)
    app.router.add_get("/rules", rules)
    app.router.add_post("/reset", reset)
    return app
