"""Own the mitmproxy, public TCP listeners and admin server lifecycle."""

from __future__ import annotations

import tempfile

from aiohttp import web
from mitmproxy import master, options
from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.tlsconfig import TlsConfig

from fault_engine.addon import FaultAddon
from fault_engine.admin import make_admin
from fault_engine.config import Config, Service
from fault_engine.engine import Engine
from fault_engine.transport import TCPBridge


class Runtime:
    """One Runtime per process: mitmproxy's context is process-global."""

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

    def _locate(self, peer: tuple) -> tuple[Service, TCPBridge] | None:
        for service in self.config.services:
            bridge = self.bridges.get(service.id)
            if bridge and bridge.owns(peer):
                return service, bridge
        return None

    def url(self, service_id: str) -> str:
        service = next(s for s in self.config.services if s.id == service_id)
        host = service.host if service.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{service.port}"

    @property
    def admin_url(self) -> str:
        host = self.config.admin.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.config.admin.port}"

    async def start(self) -> None:
        if self.master is not None:
            raise RuntimeError("already started")
        try:
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
            opts.update(connection_strategy="lazy", body_size_limit=str(self.config.body_limit))
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
                make_admin(self.config, self.engine, self.token), access_log=None
            )
            await self.admin.setup()
            await web.TCPSite(self.admin, self.config.admin.host, self.config.admin.port).start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.admin:
            await self.admin.cleanup()
            self.admin = None
        for bridge in self.bridges.values():
            await bridge.close()
        self.bridges.clear()
        await self.addon.close()
        if self.proxyserver:
            await self.proxyserver.servers.update([])
            self.proxyserver = None
        if self.master:
            await self.master.done()
            self.master = None
        if self._temp:
            self._temp.cleanup()
            self._temp = None
