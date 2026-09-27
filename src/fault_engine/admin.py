"""Separate authenticated control plane; never routed through scenario matching."""

import hmac
import json
from collections.abc import Awaitable, Callable

from aiohttp import web
from pydantic import ValidationError

from fault_engine.config import Config, Model
from fault_engine.engine import Engine
from fault_engine.plan import ActionPlan, ExecutionPlan


class CounterSelection(Model):
    service: str | None = None
    rule: str | None = None
    scope: str | None = None


def _action_summary(action: ActionPlan) -> dict[str, str | int | float]:
    summary: dict[str, str | int | float] = {"action": action.action, "repeat": action.repeat}
    if action.status is not None:
        summary.update(status=action.status, delay_seconds=action.delay_seconds)
    if action.seconds is not None:
        summary["seconds"] = action.seconds
    return summary


def make_admin(config: Config | ExecutionPlan, engine: Engine, token: str) -> web.Application:
    plan = engine.config

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
            query = dict(request.query)
            raw_limit = query.pop("limit", str(plan.limits.state_page_size))
            if not raw_limit.isascii() or not raw_limit.isdecimal():
                raise ValueError("invalid limit")
            limit = int(raw_limit)
            if not 1 <= limit <= plan.limits.state_page_size:
                raise ValueError("limit out of range")
            cursor = query.pop("cursor", None)
            selection = CounterSelection.model_validate(query)
            counters, next_cursor = engine.snapshot_page(
                service=selection.service,
                rule=selection.rule,
                scope=selection.scope,
                limit=limit,
                cursor=cursor,
            )
        except ValueError:
            return web.json_response(
                {
                    "error": "expected valid service, rule, scope, limit, cursor parameters, "
                    "once each"
                },
                status=400,
            )
        result: dict[str, object] = {"counters": counters}
        if next_cursor is not None:
            result["next_cursor"] = next_cursor
        return web.json_response(result)

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
                        "sequence": [_action_summary(a) for a in r.sequence],
                    }
                    for r in plan.rules
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
