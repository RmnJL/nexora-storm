"""
STORM Client - Inside gateway (local proxy)

Accepts local TCP connections and multiplexes them over STORM tunnel to outside server.
"""

from __future__ import annotations

import asyncio
import argparse
import logging

from storm_connection import ConnectionConfig, ConnectionManager, STORMConnection
from storm_dns import DNSGateway
from storm_failover import ResolverSelector
from storm_proto import PacketFlags, make_packet

log = logging.getLogger("storm-client")


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
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.zone = zone
        self.qtype = qtype
        self.poll_interval = max(0.05, poll_interval)
        
        # Resolver management
        self.resolver_selector = ResolverSelector(resolvers)
        self.dns_gateway = DNSGateway(
            resolvers=resolvers,
            zone=zone,
            qtype=qtype,
        )
        
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
        
        async with server:
            await server.serve_forever()
    
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
        primary, _ = self.resolver_selector.select_pair()
        
        async with tx_lock:
            try:
                # Try all resolvers in parallel and take first valid answer.
                resp, resolver, latency = await self.dns_gateway.send_to_any(
                    packet,
                    session_id=storm_conn.conn_id_hex(),
                )
                
                if resp is not None:
                    self.resolver_selector.report_success(resolver, latency)
                    log.debug(f"DNS query ok: {resolver} ({latency:.1f}ms)")
                    await storm_conn.handle_incoming_packet(resp)
                    return
                
                self.resolver_selector.report_failure(resolver, is_timeout=True)
                log.debug(f"DNS query timeout: {resolver}")
            
            except Exception as e:
                log.error(f"DNS transmission error: {e}")
                self.resolver_selector.report_failure(primary, is_timeout=False)


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
    )
    
    try:
        await client.start()
    except KeyboardInterrupt:
        print("Shutting down...")
        client.running = False


if __name__ == "__main__":
    asyncio.run(main())
