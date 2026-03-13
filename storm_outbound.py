#!/usr/bin/env python3
"""
STORM Outside Gateway (compatibility entrypoint)

This module keeps the historical `storm_outbound.py` command path alive, but
delegates the actual DNS/STORM handling to `storm_server.STORMServer`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict

from storm_server import STORMServer

log = logging.getLogger("storm-outbound")


class STORMOutbound:
    """
    Compatibility wrapper around STORMServer.

    Historically, this module had a separate DNS implementation that diverged
    from the main server path. To reduce drift and runtime inconsistencies,
    outbound now delegates to the canonical STORMServer implementation.
    """

    def __init__(
        self,
        zone: str = "t1.phonexpress.ir",
        dns_port: int = 53,
        upstream_host: str = "127.0.0.1",
        upstream_port: int = 10000,
        upstream_protocol: str = "socks5",
        listen_host: str = "0.0.0.0",
    ):
        self.zone = zone
        self.dns_port = dns_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_protocol = upstream_protocol
        self.listen_host = listen_host
        self._server = STORMServer(
            listen_host=listen_host,
            listen_port=dns_port,
            target_host=upstream_host,
            target_port=upstream_port,
            tunnel_zone=zone,
        )
    
    async def start(self) -> None:
        """Start delegated STORM DNS server."""
        if self.upstream_protocol.lower() != "socks5":
            log.warning(
                "upstream_protocol=%s is accepted for compatibility, but runtime "
                "behavior is generic TCP forwarding via STORMServer",
                self.upstream_protocol,
            )
        log.warning(
            "storm_outbound.py is running in compatibility mode via STORMServer"
        )
        await self._server.start()
    
    def get_stats(self) -> Dict:
        """Compatibility stats snapshot."""
        active = len(self._server.conn_manager.active_connections())
        return {
            "mode": "compat-wrapper",
            "active_connections": active,
            "zone": self.zone,
            "listen": f"{self.listen_host}:{self.dns_port}",
            "target": f"{self.upstream_host}:{self.upstream_port}",
        }


async def main() -> None:
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    
    dns_port = int(os.getenv("DNS_PORT", 53))
    zone = os.getenv("DNS_ZONE", "t1.phonexpress.ir")
    upstream_host = os.getenv("UPSTREAM_HOST", "127.0.0.1")
    upstream_port = int(os.getenv("UPSTREAM_PORT", 10000))
    upstream_protocol = os.getenv("UPSTREAM_PROTOCOL", "socks5")
    listen_host = os.getenv("LISTEN_HOST", "0.0.0.0")
    
    outbound = STORMOutbound(
        zone=zone,
        dns_port=dns_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        upstream_protocol=upstream_protocol,
        listen_host=listen_host,
    )
    
    async def monitor() -> None:
        while True:
            await asyncio.sleep(60)
            stats = outbound.get_stats()
            log.info(
                "Outbound stats: %s active, target=%s",
                stats["active_connections"],
                stats["target"],
            )
    
    asyncio.create_task(monitor())
    
    try:
        await outbound.start()
    except KeyboardInterrupt:
        log.info("Outbound stopped")


if __name__ == "__main__":
    asyncio.run(main())
