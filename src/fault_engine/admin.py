"""Separate authenticated control plane; never routed through scenario matching."""

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Annotated

from aiohttp import web
from pydantic import Field, ValidationError

from fault_engine.config import Config, Model
from fault_engine.engine import Engine
from fault_engine.journal import Journal
from fault_engine.plan import ActionPlan, ExecutionPlan


class CounterSelection(Model):
    service: str | None = None
    rule: str | None = None
    scope: str | None = None


class RequestSelection(CounterSelection):
    after: str | None = None


class Verification(RequestSelection):
    count: int = Field(ge=0)
    statuses: list[Annotated[int, Field(ge=100, le=599)]] | None = None
    min_interval_seconds: float | None = Field(default=None, ge=0)


class EmptyBody(Model):
    pass


def _action_summary(action: ActionPlan) -> dict[str, str | int | float]:
    summary: dict[str, str | int | float] = {"action": action.action, "repeat": action.repeat}
    if action.status is not None:
        summary.update(status=action.status, delay_seconds=action.delay_seconds)
    if action.seconds is not None:
        summary["seconds"] = action.seconds
    if action.jitter_seconds:
        summary["jitter_seconds"] = action.jitter_seconds
    return summary


def make_admin(
    config: Config | ExecutionPlan, engine: Engine, token: str, *, journal: Journal | None = None
) -> web.Application:
    plan = engine.config
    observations = journal if journal is not None else Journal(plan.limits.journal_capacity)

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
                        "probability": r.probability,
                        "seed": r.seed,
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

    async def requests(request: web.Request) -> web.Response:
        try:
            if len(request.query) != len(set(request.query)):
                raise ValueError("duplicate query parameter")
            query = dict(request.query)
            raw_limit = query.pop("limit", "100")
            if not raw_limit.isascii() or not raw_limit.isdecimal():
                raise ValueError("invalid limit")
            selection = RequestSelection.model_validate(query)
            result = observations.page(**selection.model_dump(), limit=int(raw_limit))
        except ValueError:
            return web.json_response(
                {
                    "error": "expected valid service, rule, scope, after, limit parameters, "
                    "once each"
                },
                status=400,
            )
        return web.json_response(result)

    async def clear_requests(request: web.Request) -> web.Response:
        try:
            if request.query:
                raise ValueError("unexpected query parameters")
            EmptyBody.model_validate(await request.json())
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "expected an empty JSON object"}, status=400)
        return web.json_response({"checkpoint": observations.clear()})

    async def verify(request: web.Request) -> web.Response:
        try:
            if request.query:
                raise ValueError("unexpected query parameters")
            expectation = Verification.model_validate(await request.json())
            result = observations.verify(**expectation.model_dump())
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid verification parameters"}, status=400)
        return web.json_response(result)

    app = web.Application(middlewares=[auth], client_max_size=4096)
    app.router.add_get("/health", health)
    app.router.add_get("/state", state)
    app.router.add_get("/rules", rules)
    app.router.add_post("/reset", reset)
    app.router.add_get("/requests", requests)
    app.router.add_post("/requests/reset", clear_requests)
    app.router.add_post("/verify", verify)
    return app
