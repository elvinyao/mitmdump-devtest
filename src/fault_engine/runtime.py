"""Own the mitmproxy, public TCP listeners and admin server lifecycle."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable
from typing import ClassVar

from aiohttp import web
from mitmproxy import master, options
from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.tlsconfig import TlsConfig

from fault_engine import http1_compat
from fault_engine.addon import FaultAddon
from fault_engine.admin import make_admin
from fault_engine.config import Config
from fault_engine.engine import Engine
from fault_engine.journal import Journal
from fault_engine.limits import ResourceBudget
from fault_engine.plan import ExecutionPlan, ServicePlan, compile_plan
from fault_engine.transport import TCPBridge

logger = logging.getLogger(__name__)


class Runtime:
    """One Runtime per process: mitmproxy's context is process-global."""

    _active: ClassVar[Runtime | None] = None

    def __init__(self, config: Config | ExecutionPlan, *, admin_token: str):
        if not admin_token or any(ord(c) < 33 or ord(c) > 126 for c in admin_token):
            raise ValueError("admin token must be nonempty printable ASCII without spaces")
        self.config = compile_plan(config)
        self.engine = Engine(self.config)
        self.journal = Journal(self.config.limits.journal_capacity)
        self.budget = ResourceBudget(
            max_connections=self.config.limits.max_connections,
            max_inflight_requests=self.config.limits.max_inflight_requests,
        )
        self.token = admin_token
        self.bridges: dict[str, TCPBridge] = {}
        self.master: master.Master | None = None
        self.proxyserver: Proxyserver | None = None
        self.addon = FaultAddon(
            self.config, self.engine, self._locate, budget=self.budget, journal=self.journal
        )
        self.admin: web.AppRunner | None = None
        self._temp: tempfile.TemporaryDirectory | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    def _locate(self, peer: tuple) -> tuple[ServicePlan, TCPBridge] | None:
        for service in self.config.services:
            bridge = self.bridges.get(service.id)
            if bridge and bridge.owns(peer):
                return service, bridge
        return None

    def url(self, service_id: str) -> str:
        service = next(s for s in self.config.services if s.id == service_id)
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(service.host, service.host)
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{service.port}"

    @property
    def admin_url(self) -> str:
        host = self.config.admin.host
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.config.admin.port}"

    async def start(self) -> None:
        async with self._lifecycle_lock:
            # Finish earlier cleanup before claiming ownership. No await may
            # separate this guard from the claim: Master rewrites mitmproxy.ctx.
            if Runtime._active is not None:
                raise RuntimeError("only one Runtime may be active per process")
            Runtime._active = self
            self._close_task = None
            self._cleanup_task = None
            try:
                http1_compat.install()
                await self._start()
            except BaseException as startup_error:
                # Already holding the lifecycle lock: public close() would
                # deadlock. A separate cleanup task also survives repeated
                # cancellation of start(), and close() joins the same rollback.
                try:
                    await asyncio.shield(self._begin_cleanup())
                except Exception as cleanup_error:
                    startup_error.add_note(f"Startup rollback failed: {cleanup_error!r}")
                    raise startup_error from cleanup_error
                raise

    async def _start(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="fault-engine-")
        opts = options.Options(
            mode=["reverse:http://127.0.0.1:9@127.0.0.1:0"],
            http2=False,
            rawtcp=False,
            confdir=self._temp.name,
        )
        self.master = master.Master(opts)
        self.proxyserver = Proxyserver()
        self.master.addons.add(
            Core(), self.proxyserver, NextLayer(), TlsConfig(), DnsResolver(), self.addon
        )
        opts.update(
            connection_strategy="lazy",
            body_size_limit=str(self.config.body_limit),
            keep_host_header=True,
        )
        if self.config.upstream_ca:
            opts.update(ssl_verify_upstream_trusted_ca=self.config.upstream_ca)
        if not await self.proxyserver.setup_servers():
            raise RuntimeError("could not start internal mitmproxy listener")
        await self.master.running()
        address = self.proxyserver.listen_addrs()[0]
        for service in self.config.services:
            bridge = TCPBridge("127.0.0.1", address[1], budget=self.budget)
            self.bridges[service.id] = bridge
            await bridge.start(service.host, service.port)
        self.admin = web.AppRunner(
            make_admin(self.config, self.engine, self.token, journal=self.journal),
            access_log=None,
            shutdown_timeout=0.2,
        )
        await self.admin.setup()
        await web.TCPSite(self.admin, self.config.admin.host, self.config.admin.port).start()

    async def close(self) -> None:
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        # A cancelled caller must not abandon live listeners or release process
        # ownership before cleanup finishes. Concurrent callers share this task.
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        async with self._lifecycle_lock:
            await asyncio.shield(self._begin_cleanup())

    def _begin_cleanup(self) -> asyncio.Task[None]:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup())
            self._cleanup_task.add_done_callback(self._cleanup_finished)
        return self._cleanup_task

    @staticmethod
    def _cleanup_finished(task: asyncio.Task[None]) -> None:
        # A repeatedly cancelled start() may leave no caller awaiting rollback.
        # Retrieve and report failures while preserving resources for close().
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Runtime cleanup failed; close() can retry: %s", error)

    async def _cleanup(self) -> None:
        errors: list[Exception] = []

        async def attempt(operation: Awaitable[object]) -> bool:
            try:
                await operation
            except Exception as exc:
                errors.append(exc)
                return False
            return True

        if self.admin and await attempt(self.admin.cleanup()):
            self.admin = None
        for service_id, bridge in list(self.bridges.items()):
            if await attempt(bridge.close()):
                del self.bridges[service_id]
        await attempt(self.addon.close())
        if self.proxyserver and await attempt(self.proxyserver.servers.update([])):
            self.proxyserver = None
        if self.master and await attempt(self.master.done()):
            self.master = None
        if self._temp:
            try:
                self._temp.cleanup()
            except Exception as exc:
                errors.append(exc)
            else:
                self._temp = None
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Additional cleanup failure: {error!r}")
            raise errors[0]
        # Failed/cancelled cleanup retains both failed resource references and
        # ownership. Releasing either early would let a new Master corrupt them.
        if Runtime._active is self:
            http1_compat.restore()
            Runtime._active = None
