"""HTTP fault execution; the engine never awaits and the transport owns TCP resets."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from mitmproxy import connection, http

from fault_engine.config import TOKEN, Config
from fault_engine.engine import Decision, Engine, ScenarioError
from fault_engine.journal import Journal
from fault_engine.limits import Lease, ResourceBudget
from fault_engine.plan import ActionPlan, ExecutionPlan, ServicePlan
from fault_engine.transport import TCPBridge

logger = logging.getLogger(__name__)


class FaultAddon:
    def __init__(
        self,
        config: Config | ExecutionPlan,
        engine: Engine,
        locate: Callable[[tuple], tuple[ServicePlan, TCPBridge] | None],
        *,
        budget: ResourceBudget | None = None,
        journal: Journal | None = None,
    ):
        self.config = engine.config
        self.engine = engine
        self.locate = locate
        self.pending: set[asyncio.Task] = set()
        self.control_headers = {r.scope.lower() for r in self.config.rules if r.scope != "global"}
        self.budget = budget or ResourceBudget(
            max_connections=self.config.limits.max_connections,
            max_inflight_requests=self.config.limits.max_inflight_requests,
        )
        self._requests: dict[str, tuple[str, Lease]] = {}
        self._flow_tasks: dict[str, asyncio.Task] = {}
        self._seen_clients: set[str] = set()
        self.journal = (
            journal if journal is not None else Journal(self.config.limits.journal_capacity)
        )

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
                    "sampled": decision.sampled if decision else None,
                    "delay_seconds": (
                        decision.action.seconds
                        if decision and decision.action.seconds is not None
                        else decision.action.delay_seconds
                        if decision
                        else None
                    ),
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
        for flow_id in self._requests:
            self.journal.finish(flow_id, "shutdown")
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for _, lease in self._requests.values():
            lease.release()
        self._requests.clear()
        self._flow_tasks.clear()
        self._seen_clients.clear()

    def _release_request(self, flow_id: str) -> None:
        entry = self._requests.pop(flow_id, None)
        if entry is not None:
            entry[1].release()

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        if flow.id in self._requests or flow.metadata.get("fault_capacity_rejected"):
            return
        first_request = flow.client_conn.id not in self._seen_clients
        self._seen_clients.add(flow.client_conn.id)
        lease = self.budget.requests.acquire()
        if lease is not None:
            self._requests[flow.id] = (flow.client_conn.id, lease)
            return
        flow.metadata["fault_capacity_rejected"] = True
        self._problem(flow, 503, "in-flight request capacity exhausted")
        assert flow.response is not None
        flow.response.headers["X-Fault-Engine-Error"] = "capacity"
        flow.response.headers["Connection"] = "close"
        found = self.locate(flow.client_conn.peername)
        if found is not None:
            flow.metadata["fault_service"] = found[0].id
        self._event(flow, "request", "capacity_exhausted")
        # mitmproxy buffers bodies even when requestheaders sets a response. Send
        # this small final rejection through the owned socket before buffering.
        if found is not None:
            body = flow.response.raw_content or b""
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"X-Fault-Engine-Error: capacity\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + (b"" if flow.request.data.method == b"HEAD" else body)
            )
            try:
                # The previous response on a reused HTTP/1 connection may still
                # be in the relay buffer. Appending an early 503 could corrupt
                # its body; an explicit transport close is safe in that case.
                await found[1].reject(flow.client_conn.peername, response if first_request else b"")
            finally:
                if flow.killable:
                    flow.kill()
        elif flow.killable:
            flow.kill()

    def client_disconnected(self, client: connection.Client) -> None:
        self._seen_clients.discard(client.id)
        for flow_id, (client_id, _) in list(self._requests.items()):
            if client_id != client.id:
                continue
            self.journal.finish(flow_id, "client_disconnected")
            task = self._flow_tasks.get(flow_id)
            if task is not None and not task.done():
                task.cancel()
            else:
                self._release_request(flow_id)

    async def _respond(self, flow: http.HTTPFlow, action: ActionPlan) -> None:
        if action.delay_seconds:
            await self._sleep(action.delay_seconds)
        # Response.make(content=...) applies Content-Encoding automatically. Scenario
        # bodies already describe the wire representation, including compressed bytes.
        assert action.status is not None
        flow.response = http.Response.make(
            action.status,
            b"",
            action.wire_headers,
        )
        content = action.body_bytes()
        flow.response.raw_content = content
        flow.response.headers["Content-Length"] = str(len(content))
        if action.status in {204, 304}:
            flow.response.headers.pop("content-length", None)

    def _disconnect(self, flow: http.HTTPFlow, action: ActionPlan) -> bool:
        if action.action in {"reset", "reset_after"}:
            found = self.locate(flow.client_conn.peername)
            if found is None or not found[1].reset(flow.client_conn.peername):
                self._problem(flow, 503, "TCP reset failed: connection mapping unavailable")
                self._event(flow, "fault", "reset_failed")
                self.journal.finish(flow.id, "reset_failed", status=503)
                return False
        self.journal.finish(flow.id, action.action)
        flow.kill()
        self._event(flow, "fault", action.action)
        return True

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("fault_capacity_rejected"):
            return
        task = asyncio.current_task()
        assert task is not None
        self._flow_tasks[flow.id] = task
        try:
            await self._request(flow)
        except BaseException as exc:
            self.journal.finish(
                flow.id,
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error",
            )
            self._release_request(flow.id)
            raise
        finally:
            self._flow_tasks.pop(flow.id, None)

    async def _request(self, flow: http.HTTPFlow) -> None:
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
        self.journal.start(
            flow.id,
            service=service.id,
            rule=decision.rule_id if decision else None,
            scope=decision.scope if decision else None,
            method=method,
            ordinal=decision.ordinal if decision else None,
            action=decision.action.action if decision else "passthrough",
            sampled=decision.sampled if decision else None,
        )
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
        if action.action == "respond":
            await self._respond(flow, action)
        elif action.action in {"delay_before", "timeout"}:
            assert action.seconds is not None
            await self._sleep(action.seconds)
            if action.action == "timeout":
                self.journal.finish(flow.id, "timeout")
                flow.kill()
                self._event(flow, "request", "hold_expired")
        elif action.action in {"reset", "disconnect"}:
            self._disconnect(flow, action)

    async def response(self, flow: http.HTTPFlow) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._flow_tasks[flow.id] = task
        try:
            await self._response(flow)
        except BaseException as exc:
            self.journal.finish(
                flow.id,
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error",
            )
            raise
        finally:
            self._flow_tasks.pop(flow.id, None)
            self._release_request(flow.id)

    async def _response(self, flow: http.HTTPFlow) -> None:
        decision: Decision | None = flow.metadata.get("fault_decision")
        if flow.response is not None and (decision is None or decision.action.action != "respond"):
            self.journal.mark_upstream(flow.id)
        if decision:
            action = decision.action
            if action.action in {"delay_after", "respond_after", "reset_after", "disconnect_after"}:
                self._event(flow, "response", "upstream_received")
                if action.action == "delay_after":
                    assert action.seconds is not None
                    await self._sleep(action.seconds)
                elif action.action == "respond_after":
                    await self._respond(flow, action)
                elif action.action in {"reset_after", "disconnect_after"}:
                    if self._disconnect(flow, action):
                        return
        method = flow.request.data.method
        if method == b"HEAD" and flow.response is not None:
            # Preserve the representation length while emitting no body on the wire.
            flow.response.raw_content = b""
        elif method.upper() == b"HEAD" and flow.response is not None:
            # These are distinct custom methods. mitmproxy 12 also suppresses the
            # final chunk for their responses, so frame the fully buffered body
            # with its length instead. The compatibility adapter preserves the
            # upstream body while leaving the actual request method untouched.
            if "chunked" in flow.response.headers.get("transfer-encoding", "").lower():
                flow.response.headers.pop("transfer-encoding", None)
                flow.response.headers.pop("trailer", None)
                if flow.response.status_code not in {204, 304}:
                    flow.response.headers["Content-Length"] = str(
                        len(flow.response.raw_content or b"")
                    )
        self._event(
            flow, "response", str(flow.response.status_code) if flow.response else "missing"
        )
        self.journal.finish(
            flow.id,
            "response_prepared" if flow.response else "missing_response",
            status=flow.response.status_code if flow.response else None,
        )

    def error(self, flow: http.HTTPFlow) -> None:
        self.journal.finish(flow.id, "transport_error")
        self._release_request(flow.id)
        self._event(flow, "error", "transport_error")
