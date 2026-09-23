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

    async def _respond(self, flow: http.HTTPFlow, action: Respond) -> None:
        if action.delay_seconds:
            await self._sleep(action.delay_seconds)
        headers = dict(action.headers)
        if "json_body" in action.model_fields_set and not any(
            k.lower() == "content-type" for k in headers
        ):
            headers["Content-Type"] = "application/json"
        # Response.make(content=...) applies Content-Encoding automatically. Scenario
        # bodies already describe the wire representation, including compressed bytes.
        flow.response = http.Response.make(
            action.status,
            b"",
            [(name.encode("ascii"), value.encode("latin-1")) for name, value in headers.items()],
        )
        content = action.body_bytes()
        flow.response.raw_content = content
        flow.response.headers["Content-Length"] = str(len(content))
        if action.status in {204, 304}:
            flow.response.headers.pop("content-length", None)

    def _disconnect(self, flow: http.HTTPFlow, action: Disconnect) -> bool:
        if action.action in {"reset", "reset_after"}:
            found = self.locate(flow.client_conn.peername)
            if found is None or not found[1].reset(flow.client_conn.peername):
                self._problem(flow, 503, "TCP reset failed: connection mapping unavailable")
                self._event(flow, "fault", "reset_failed")
                return False
        flow.kill()
        self._event(flow, "fault", action.action)
        return True

    async def request(self, flow: http.HTTPFlow) -> None:
        # mitmproxy's convenience getter uppercases method names; HTTP tokens are
        # case sensitive and custom method rules must match the actual wire token.
        method = flow.request.data.method.decode("ascii", "surrogateescape")
        if not TOKEN.fullmatch(method):
            self._problem(flow, 400, "invalid HTTP method")
            return
        found = self.locate(flow.client_conn.peername)
        if found is None:
            self._problem(flow, 503, "unmapped client connection")
            return
        service, _ = found
        flow.metadata["fault_service"] = service.id
        if any(len(flow.request.headers.get_all(name)) > 1 for name in self.control_headers):
            self._problem(flow, 400, "test scope header must occur only once")
            self._event(flow, "request", "scenario_error")
            return
        path = flow.request.path.split("?", 1)[0]
        try:
            decision = self.engine.decide(
                service.id,
                method,
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
        flow.request.authority = ""
        flow.request.headers["Host"] = url.netloc
        for name in self.control_headers:
            flow.request.headers.pop(name, None)
        flow.metadata["fault_decision"] = decision
        self._event(flow, "request", "selected")
        if decision is None:
            return
        action = decision.action
        if isinstance(action, Respond) and action.action == "respond":
            await self._respond(flow, action)
        elif isinstance(action, Delay) and action.action != "delay_after":
            await self._sleep(action.seconds)
            if action.action == "timeout":
                flow.kill()
                self._event(flow, "request", "hold_expired")
        elif isinstance(action, Disconnect) and action.action in {"reset", "disconnect"}:
            self._disconnect(flow, action)

    async def response(self, flow: http.HTTPFlow) -> None:
        decision: Decision | None = flow.metadata.get("fault_decision")
        if decision:
            action = decision.action
            if action.action in {"delay_after", "respond_after", "reset_after", "disconnect_after"}:
                self._event(flow, "response", "upstream_received")
                if isinstance(action, Delay):
                    await self._sleep(action.seconds)
                elif isinstance(action, Respond):
                    await self._respond(flow, action)
                elif isinstance(action, Disconnect):
                    if self._disconnect(flow, action):
                        return
        if flow.request.method == "HEAD" and flow.response is not None:
            # Preserve the representation length while emitting no body on the wire.
            flow.response.raw_content = b""
        self._event(
            flow, "response", str(flow.response.status_code) if flow.response else "missing"
        )

    def error(self, flow: http.HTTPFlow) -> None:
        self._event(flow, "error", "transport_error")
