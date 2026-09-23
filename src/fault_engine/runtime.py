"""Own the mitmproxy, public TCP listeners and admin server lifecycle."""

from __future__ import annotations

import asyncio
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
from fault_engine.config import Config, Service
from fault_engine.engine import Engine
from fault_engine.transport import TCPBridge


class Runtime:
    """One Runtime per process: mitmproxy's context is process-global."""

    _active: ClassVar[Runtime | None] = None

    def __init__(self, config: Config, *, admin_token: str):
        if not admin_token or any(ord(c) < 33 or ord(c) > 126 for c in admin_token):
            raise ValueError("admin token must be nonempty printable ASCII without spaces")
        self.config = config
        self.engine = Engine(config)
        self.token = admin_token
        self.bridges: dict[str, TCPBridge] = {}
        self.master: master.Master | None = None
        self.proxyserver: Proxyserver | None = None
        self.addon = FaultAddon(config, self.engine, self._locate)
        self.admin: web.AppRunner | None = None
        self._temp: tempfile.TemporaryDirectory | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    def _locate(self, peer: tuple) -> tuple[Service, TCPBridge] | None:
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
            try:
                http1_compat.install()
                await self._start()
            except BaseException:
                # Already holding the lifecycle lock: public close() would
                # wait for this start() and deadlock the rollback.
                await self._cleanup()
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
            bridge = TCPBridge("127.0.0.1", address[1])
            self.bridges[service.id] = bridge
            await bridge.start(service.host, service.port)
        self.admin = web.AppRunner(
            make_admin(self.config, self.engine, self.token),
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
            await self._cleanup()

    async def _cleanup(self) -> None:
        errors: list[Exception] = []

        async def attempt(operation: Awaitable[object]) -> None:
            try:
                await operation
            except Exception as exc:
                errors.append(exc)

        try:
            if self.admin:
                await attempt(self.admin.cleanup())
                self.admin = None
            for bridge in self.bridges.values():
                await attempt(bridge.close())
            self.bridges.clear()
            await attempt(self.addon.close())
            if self.proxyserver:
                await attempt(self.proxyserver.servers.update([]))
                self.proxyserver = None
            if self.master:
                await attempt(self.master.done())
                self.master = None
            if self._temp:
                try:
                    self._temp.cleanup()
                except Exception as exc:
                    errors.append(exc)
                self._temp = None
        finally:
            if Runtime._active is self:
                try:
                    http1_compat.restore()
                finally:
                    Runtime._active = None
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Additional cleanup failure: {error!r}")
            raise errors[0]
