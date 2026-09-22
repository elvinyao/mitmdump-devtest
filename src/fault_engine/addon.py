"""HTTP fault execution; the engine never awaits and the transport owns TCP resets."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from mitmproxy import http

from fault_engine.config import TOKEN, Config, Delay, Disconnect, Respond, Service
from fault_engine.engine import Decision, Engine, ScenarioError
from fault_engine.transport import TCPBridge

logger = logging.getLogger(__name__)


class FaultAddon:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        locate: Callable[[tuple], tuple[Service, TCPBridge] | None],
    ):
        self.config = config
        self.engine = engine
        self.locate = locate
        self.pending: set[asyncio.Task] = set()
        self.control_headers = {r.scope.lower() for r in config.rules if r.scope != "global"}

    @staticmethod
    def _event(flow: http.HTTPFlow, phase: str, result: str) -> None:
        decision: Decision | None = flow.metadata.get("fault_decision")
        logger.info(
            json.dumps(
                {
                    "time": time.time(),
                    "request_id": flow.id,
                    "service": flow.metadata.get("fault_service"),
                    "rule": decision.rule_id if decision else None,
                    "scope": decision.scope[:64] if decision else None,
                    "ordinal": decision.ordinal if decision else None,
                    "action": decision.action.action if decision else "passthrough",
                    "phase": phase,
                    "result": result,
                },
                ensure_ascii=True,
            )
        )

    @staticmethod
    def _problem(flow: http.HTTPFlow, status: int, message: str) -> None:
        flow.response = http.Response.make(
            status,
            json.dumps({"error": message}).encode(),
            {"Content-Type": "application/json", "X-Fault-Engine-Error": "scenario"},
        )

    async def _sleep(self, seconds: float) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.pending.add(task)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.pending.discard(task)

    async def close(self) -> None:
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def request(self, flow: http.HTTPFlow) -> None:
        if not TOKEN.fullmatch(flow.request.method):
            self._problem(flow, 400, "invalid HTTP method")
            return
        found = self.locate(flow.client_conn.peername)
        if found is None:
            self._problem(flow, 503, "unmapped client connection")
            return
        service, bridge = found
        flow.metadata["fault_service"] = service.id
        path = flow.request.path.split("?", 1)[0]
        try:
            decision = self.engine.decide(
                service.id,
                flow.request.method,
                path,
                dict(flow.request.headers),
                list(flow.request.query.items(multi=True)),
            )
        except ScenarioError as exc:
            self._problem(flow, 400, str(exc))
            self._event(flow, "request", "scenario_error")
            return
        url = urlsplit(service.upstream)
        flow.request.scheme = url.scheme
        flow.request.host = url.hostname or ""
        flow.request.port = url.port or (443 if url.scheme == "https" else 80)
        flow.request.headers["Host"] = url.netloc
        for name in self.control_headers:
            flow.request.headers.pop(name, None)
        flow.metadata["fault_decision"] = decision
        self._event(flow, "request", "selected")
        if decision is None:
            return
        action = decision.action
        if isinstance(action, Respond):
            headers = dict(action.headers)
            if "json_body" in action.model_fields_set and not any(
                k.lower() == "content-type" for k in headers
            ):
                headers["Content-Type"] = "application/json"
            flow.response = http.Response.make(action.status, action.body_bytes(), headers)
            if action.status in {204, 304}:
                flow.response.headers.pop("content-length", None)
        elif isinstance(action, Delay) and action.action != "delay_after":
            await self._sleep(action.seconds)
            if action.action == "timeout":
                flow.kill()
                self._event(flow, "request", "hold_expired")
        elif isinstance(action, Disconnect):
            if action.action == "reset" and not bridge.reset(flow.client_conn.peername):
                self._problem(flow, 503, "TCP reset failed: connection mapping unavailable")
                self._event(flow, "request", "reset_failed")
                return
            flow.kill()
            self._event(flow, "request", action.action)

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.request.method == "HEAD" and flow.response is not None:
            # Preserve the representation length while emitting no body on the wire.
            flow.response.raw_content = b""
        decision: Decision | None = flow.metadata.get("fault_decision")
        if (
            decision
            and isinstance(decision.action, Delay)
            and decision.action.action == "delay_after"
        ):
            self._event(flow, "response", "upstream_received")
            await self._sleep(decision.action.seconds)
        self._event(
            flow, "response", str(flow.response.status_code) if flow.response else "missing"
        )

    def error(self, flow: http.HTTPFlow) -> None:
        self._event(flow, "error", "transport_error")
