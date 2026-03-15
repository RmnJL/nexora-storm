"""
STORM Client - Inside gateway (local proxy)

Accepts local TCP connections and multiplexes them over STORM tunnel to outside server.
"""

from __future__ import annotations

import asyncio
import argparse
import ipaddress
import logging
import os

from storm_connection import ConnectionConfig, ConnectionManager, STORMConnection
from storm_dns import DNSGateway
from storm_failover import ResolverSelector
from storm_proto import PacketFlags, make_packet

log = logging.getLogger("storm-client")


def parse_resolver_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in text.replace("\n", " ").split(" "):
        ip = token.strip()
        if not ip or ip in seen:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version != 4 or not addr.is_global:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def load_resolvers_file(path: str, min_selected: int, max_selected: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    selected = parse_resolver_tokens(raw)
    if not selected:
        return []
    capped = selected[: max(1, int(max_selected))]
    if len(capped) < max(1, int(min_selected)):
        return []
    return capped


class STORMClient:
    """STORM client gateway"""
    
    def __init__(
        self,
        resolvers: list[str],
        listen_host: str = "127.0.0.1",
        listen_port: int = 1443,
        zone: str = "t1.phonexpress.ir",
        qtype: str = "TXT",
        poll_interval: float = 0.2,
        dns_timeout: float = 3.0,
        resolver_fanout: int = 3,
        resolvers_watch_file: str = "",
        resolvers_watch_interval: float = 2.0,
        resolvers_watch_min: int = 2,
        resolvers_watch_max: int = 8,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.zone = zone
        self.qtype = qtype
        self.poll_interval = max(0.05, poll_interval)
        self.dns_timeout = max(0.5, float(dns_timeout))
        self.resolver_fanout = max(1, int(resolver_fanout))
        self.resolvers_watch_file = str(resolvers_watch_file or "").strip()
        self.resolvers_watch_interval = max(0.3, float(resolvers_watch_interval))
        self.resolvers_watch_min = max(1, int(resolvers_watch_min))
        self.resolvers_watch_max = max(self.resolvers_watch_min, int(resolvers_watch_max))

        # Resolver management
        initial = parse_resolver_tokens(" ".join(resolvers)) or list(resolvers)
        self.resolver_selector = ResolverSelector(initial)
        self.dns_gateway = DNSGateway(
            resolvers=initial,
            zone=zone,
            qtype=qtype,
        )
        self._current_resolvers = list(initial)
        self._resolver_watch_task: asyncio.Task | None = None
        self._resolver_watch_last_mtime: float = 0.0

        # Connection management
        self.conn_config = ConnectionConfig()
        self.conn_manager = ConnectionManager(self.conn_config)
        
        self.running = False
    
    async def start(self) -> None:
        """Start listening for connections"""
        log.info(f"STORM client listening on {self.listen_host}:{self.listen_port}")
        
        server = await asyncio.start_server(
            self.handle_connection,
            self.listen_host,
            self.listen_port,
        )
        
        self.running = True
        if self.resolvers_watch_file:
            self._resolver_watch_task = asyncio.create_task(self._watch_resolvers_file())

        try:
            async with server:
                await server.serve_forever()
        finally:
            self.running = False
            if self._resolver_watch_task:
                self._resolver_watch_task.cancel()
                await asyncio.gather(self._resolver_watch_task, return_exceptions=True)

    async def _watch_resolvers_file(self) -> None:
        """Hot-reload resolver set from file without restarting storm-client."""
        while self.running:
            try:
                try:
                    mtime = os.path.getmtime(self.resolvers_watch_file)
                except OSError:
                    mtime = 0.0
                if mtime != self._resolver_watch_last_mtime:
                    self._resolver_watch_last_mtime = mtime
                    selected = load_resolvers_file(
                        path=self.resolvers_watch_file,
                        min_selected=self.resolvers_watch_min,
                        max_selected=self.resolvers_watch_max,
                    )
                    if selected:
                        changed_sel = self.resolver_selector.replace_resolvers(selected)
                        changed_dns = self.dns_gateway.set_resolvers(selected)
                        if changed_sel or changed_dns:
                            self._current_resolvers = list(selected)
                            log.info(
                                "hot-reloaded resolvers count=%d first=%s",
                                len(selected),
                                selected[0],
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("resolver watch failed: %s", exc)

            await asyncio.sleep(self.resolvers_watch_interval)
    
    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming TCP connection"""
        peer_addr = writer.get_extra_info("peername")
        log.info(f"accept connection from {peer_addr}")
        
        # Create STORM connection for this user connection
        storm_conn = self.conn_manager.create_connection()
        log.info(f"opened STORM connection {storm_conn.conn_id_hex()}")
        stop_event = asyncio.Event()
        tx_lock = asyncio.Lock()
        tasks: list[asyncio.Task] = []
        
        try:
            # Run IO + DNS pump together so downstream responses continue
            # even while local side is temporarily idle.
            tasks = [
                asyncio.create_task(
                    self._forward_to_storm(reader, storm_conn, stop_event)
                ),
                asyncio.create_task(
                    self._forward_from_storm(writer, storm_conn, stop_event)
                ),
                asyncio.create_task(
                    self._dns_pump(storm_conn, stop_event, tx_lock)
                ),
            ]
            await asyncio.gather(*tasks)
        
        except Exception as e:
            log.error(f"connection error: {e}")
        
        finally:
            stop_event.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await storm_conn.close()
            self.conn_manager.remove_connection(storm_conn.conn_id)
            log.info(f"closed connection from {peer_addr}")
            writer.close()
            await writer.wait_closed()
    
    async def _forward_to_storm(
        self,
        reader: asyncio.StreamReader,
        storm_conn: STORMConnection,
        stop_event: asyncio.Event,
    ) -> None:
        """Forward TCP data to STORM tunnel"""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                
                log.debug(f"forward {len(data)} bytes to STORM")
                await storm_conn.send_data(data)
        
        except Exception as e:
            log.error(f"forward to STORM error: {e}")
        finally:
            stop_event.set()
    
    async def _forward_from_storm(
        self,
        writer: asyncio.StreamWriter,
        storm_conn: STORMConnection,
        stop_event: asyncio.Event,
    ) -> None:
        """Forward STORM data to TCP"""
        idle_rounds_after_stop = 0
        try:
            while not writer.is_closing():
                # Poll for ordered data
                data = await storm_conn.get_ordered_data(timeout=self.poll_interval)
                
                if data:
                    log.debug(f"forward {len(data)} bytes from STORM")
                    writer.write(data)
                    await writer.drain()
                    idle_rounds_after_stop = 0
                    continue
                
                if stop_event.is_set():
                    idle_rounds_after_stop += 1
                    if idle_rounds_after_stop >= 3:
                        break
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"forward from STORM error: {e}")
    
    async def _dns_pump(
        self,
        storm_conn: STORMConnection,
        stop_event: asyncio.Event,
        tx_lock: asyncio.Lock,
    ) -> None:
        """
        Keep issuing DNS queries for this connection.
        This keeps downstream flow alive even when upstream is temporarily idle.
        """
        try:
            while True:
                had_outgoing = await self._transmit_from_queue(
                    storm_conn=storm_conn,
                    tx_lock=tx_lock,
                )
                
                if stop_event.is_set() and not had_outgoing:
                    break
                
                if had_outgoing:
                    continue
                
                # Poll server side queue with keepalive.
                keepalive_packet = make_packet(
                    conn_id=storm_conn.conn_id,
                    flags=PacketFlags.KEEPALIVE,
                    seq_offset=0,
                    payload=b"",
                )
                await self._send_packet_via_dns(
                    storm_conn=storm_conn,
                    packet=keepalive_packet,
                    tx_lock=tx_lock,
                )
                await asyncio.sleep(self.poll_interval)
        
        except asyncio.CancelledError:
            pass
    
    async def _transmit_from_queue(
        self,
        storm_conn: STORMConnection,
        tx_lock: asyncio.Lock,
    ) -> bool:
        """Send queued packets via DNS. Returns True if at least one packet was sent."""
        transmitted = False
        while True:
            packet = storm_conn.get_next_outgoing()
            if not packet:
                break
            transmitted = True
            await self._send_packet_via_dns(storm_conn, packet, tx_lock)
        
        return transmitted
    
    async def _send_packet_via_dns(
        self,
        storm_conn: STORMConnection,
        packet: bytes,
        tx_lock: asyncio.Lock,
    ) -> None:
        """Send one packet over DNS and feed response back into connection state."""
        async with tx_lock:
            try:
                ordered = self.resolver_selector.rank_candidates()
                fanout = min(self.resolver_fanout, max(1, len(ordered)))
                result = await self.dns_gateway.send_to_any_detailed(
                    packet,
                    session_id=storm_conn.conn_id_hex(),
                    timeout=self.dns_timeout,
                    resolver_order=ordered,
                    fanout=fanout,
                )
                
                if result.response_packet is not None:
                    self.resolver_selector.report_success(result.resolver, result.latency_ms)
                    log.debug(
                        "DNS query ok: resolver=%s fanout=%d latency=%.1fms",
                        result.resolver,
                        fanout,
                        result.latency_ms,
                    )
                    await storm_conn.handle_incoming_packet(result.response_packet)
                    return
                
                is_timeout = result.error_class in {"timeout", "no-response"}
                attempted = list(result.attempted_resolvers) if result.attempted_resolvers else [result.resolver]
                for resolver in attempted:
                    self.resolver_selector.report_failure(
                        resolver,
                        is_timeout=is_timeout,
                        latency_ms=result.latency_ms,
                        reason=result.error_class,
                    )
                log.debug(
                    "DNS query failed: reason=%s resolver=%s attempted=%s detail=%s",
                    result.error_class,
                    result.resolver,
                    ",".join(attempted),
                    result.error_detail,
                )
            
            except Exception as e:
                log.error(f"DNS transmission error: {e}")
                primary = self.resolver_selector.select_primary()
                self.resolver_selector.report_failure(
                    primary,
                    is_timeout=False,
                    reason="client-exception",
                )


async def main():
    """Example client usage"""
    parser = argparse.ArgumentParser(description="STORM client gateway")
    parser.add_argument("--listen", default="127.0.0.1:1443", help="Listen address host:port")
    parser.add_argument(
        "--resolvers",
        nargs="+",
        default=["185.49.84.2", "178.22.122.100"],
        help="Resolver IPs (space-separated)",
    )
    parser.add_argument("--zone", default="t1.phonexpress.ir", help="Tunnel DNS zone")
    parser.add_argument("--qtype", default="TXT", help="DNS query type")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.2,
        help="Idle poll interval in seconds for DNS keepalive queries",
    )
    parser.add_argument(
        "--dns-timeout",
        type=float,
        default=3.0,
        help="Per-query DNS timeout in seconds",
    )
    parser.add_argument(
        "--resolver-fanout",
        type=int,
        default=3,
        help="How many resolvers to query in parallel per retry window",
    )
    parser.add_argument(
        "--resolvers-watch-file",
        default="",
        help="Optional file to hot-reload resolver list from",
    )
    parser.add_argument(
        "--resolvers-watch-interval",
        type=float,
        default=2.0,
        help="Hot-reload check interval in seconds",
    )
    parser.add_argument(
        "--resolvers-watch-min",
        type=int,
        default=2,
        help="Minimum resolvers required in watch file before applying",
    )
    parser.add_argument(
        "--resolvers-watch-max",
        type=int,
        default=8,
        help="Maximum resolvers loaded from watch file",
    )
    args = parser.parse_args()
    
    listen_host, listen_port = args.listen.rsplit(":", 1)
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    client = STORMClient(
        resolvers=args.resolvers,
        listen_host=listen_host,
        listen_port=int(listen_port),
        zone=args.zone,
        qtype=args.qtype,
        poll_interval=args.poll_interval,
        dns_timeout=args.dns_timeout,
        resolver_fanout=args.resolver_fanout,
        resolvers_watch_file=args.resolvers_watch_file,
        resolvers_watch_interval=args.resolvers_watch_interval,
        resolvers_watch_min=args.resolvers_watch_min,
        resolvers_watch_max=args.resolvers_watch_max,
    )
    
    try:
        await client.start()
    except KeyboardInterrupt:
        print("Shutting down...")
        client.running = False


if __name__ == "__main__":
    asyncio.run(main())
