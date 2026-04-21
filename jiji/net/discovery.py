"""LAN peer discovery over mDNS / DNS-SD.

Advertises the local node as `_jiji._tcp.local.` with TXT metadata
describing the P2P port, chain height, and genesis prefix. When another
node publishes a compatible service, `connect_to_peer` is invoked.

This is a LAN-only convenience: the advertised address is a link-local
interface, and multicast DNS does not traverse routers. If mDNS is
blocked on the network (corporate guest Wi-Fi is a common offender),
fall back to the `--peers` CLI flag.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

from jiji.core.config import MDNS_SERVICE_TYPE

if TYPE_CHECKING:
    from jiji.net.server import P2PServer

logger = logging.getLogger(__name__)


def _pick_lan_ip() -> str:
    """Best-effort: pick the outbound interface's IP without actually sending."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class LANDiscovery:
    """Zeroconf-based LAN discovery wrapper around P2PServer."""

    def __init__(self, p2p: P2PServer, genesis_hash: bytes, instance_name: str | None = None):
        self._p2p = p2p
        self._genesis_prefix = genesis_hash[:8].hex()
        self._instance = instance_name or f"jiji-{genesis_hash[:3].hex()}-{p2p.port}"
        self._azc = None
        self._service_info = None
        self._browser = None
        self._listener = None
        self._our_addresses: set[tuple[str, int]] = set()
        self._last_height_advertised: int | None = None
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
        from zeroconf import ServiceInfo

        local_ip = _pick_lan_ip()
        self._our_addresses.add((local_ip, self._p2p.port))
        self._our_addresses.add(("127.0.0.1", self._p2p.port))

        self._azc = AsyncZeroconf()
        height = self._p2p.node.chain.height
        self._last_height_advertised = height

        full_name = f"{self._instance}.{MDNS_SERVICE_TYPE}"
        self._service_info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            full_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self._p2p.port,
            properties={
                "genesis": self._genesis_prefix,
                "height": str(height),
                "version": "1",
            },
            server=f"{self._instance}.local.",
        )
        try:
            await self._azc.async_register_service(self._service_info)
        except Exception as e:
            logger.warning(f"mDNS register failed: {e}")
            await self._azc.async_close()
            self._azc = None
            return

        self._listener = _DiscoveryListener(self)
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, MDNS_SERVICE_TYPE, listener=self._listener,
        )
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(f"mDNS advertising {self._instance} at {local_ip}:{self._p2p.port}")

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._azc is not None:
            if self._service_info is not None:
                try:
                    await self._azc.async_unregister_service(self._service_info)
                except Exception:
                    pass
            await self._azc.async_close()

    async def _refresh_loop(self) -> None:
        """Re-advertise when chain height changes materially, so peers see fresh state."""
        while True:
            await asyncio.sleep(30)
            if self._azc is None or self._service_info is None:
                continue
            current = self._p2p.node.chain.height
            if self._last_height_advertised is None:
                self._last_height_advertised = current
                continue
            if current - self._last_height_advertised < 10:
                continue
            self._service_info.properties = {
                b"genesis": self._genesis_prefix.encode(),
                b"height": str(current).encode(),
                b"version": b"1",
            }
            try:
                await self._azc.async_update_service(self._service_info)
                self._last_height_advertised = current
            except Exception as e:
                logger.debug(f"mDNS update failed: {e}")

    async def _on_peer_discovered(self, host: str, port: int, genesis_prefix: str) -> None:
        if (host, port) in self._our_addresses:
            return
        if genesis_prefix and genesis_prefix != self._genesis_prefix:
            logger.debug(
                f"mDNS peer {host}:{port} genesis mismatch ({genesis_prefix} != {self._genesis_prefix})"
            )
            return
        logger.info(f"mDNS discovered peer {host}:{port}")
        try:
            await self._p2p.connect_to_peer(host, port)
        except Exception as e:
            logger.debug(f"mDNS auto-connect to {host}:{port} failed: {e}")


class _DiscoveryListener:
    """Bridges Zeroconf's sync callbacks into our async handler."""

    def __init__(self, parent: LANDiscovery):
        self._parent = parent
        self._loop = asyncio.get_event_loop()

    def add_service(self, zc, service_type, name):
        asyncio.run_coroutine_threadsafe(
            self._resolve_and_dispatch(zc, service_type, name), self._loop,
        )

    def update_service(self, zc, service_type, name):
        # New height etc. — no action required for now.
        pass

    def remove_service(self, zc, service_type, name):
        pass

    async def _resolve_and_dispatch(self, zc, service_type, name) -> None:
        from zeroconf.asyncio import AsyncServiceInfo

        info = AsyncServiceInfo(service_type, name)
        try:
            ok = await info.async_request(zc, 3000)
        except Exception as e:
            logger.debug(f"mDNS resolve failed for {name}: {e}")
            return
        if not ok:
            return
        addresses = info.parsed_addresses() if info else []
        if not addresses:
            return
        port = info.port or 0
        if port <= 0:
            return
        genesis_prefix = ""
        props = info.properties or {}
        raw = props.get(b"genesis") or props.get("genesis")
        if isinstance(raw, bytes):
            genesis_prefix = raw.decode("ascii", errors="replace")
        elif isinstance(raw, str):
            genesis_prefix = raw
        for host in addresses:
            await self._parent._on_peer_discovered(host, port, genesis_prefix)
