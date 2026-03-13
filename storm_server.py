"""
STORM Server - Outside gateway

Receives DNS queries carrying STORM packets and forwards to target services.
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import socket
from typing import Optional

from storm_connection import ConnectionConfig, ConnectionManager, STORMConnection
from storm_dns_server import STORMDNSServer
from storm_proto import PacketFlags, make_packet, parse_packet

log = logging.getLogger("storm-server")


class TargetConnector:
    """Manages TCP connections to target services"""
    
    def __init__(self, target_host: str, target_port: int):
        self.target_host = target_host
        self.target_port = target_port
        self._sockets: dict[str, tuple] = {}  # conn_id_hex -> (reader, writer)
    
    async def connect(self, conn_id_hex: str) -> Optional[tuple]:
        """Connect to target service"""
        try:
            reader, writer = await asyncio.open_connection(
                self.target_host,
                self.target_port,
            )
            self._sockets[conn_id_hex] = (reader, writer)
            log.info(f"connected to target {self.target_host}:{self.target_port}")
            return reader, writer
        
        except Exception as e:
            log.error(f"target connection failed: {e}")
            return None
    
    def get_connection(self, conn_id_hex: str) -> Optional[tuple]:
        """Get existing target connection"""
        return self._sockets.get(conn_id_hex)
    
    def close_connection(self, conn_id_hex: str) -> None:
        """Close target connection"""
        if conn_id_hex in self._sockets:
            reader, writer = self._sockets[conn_id_hex]
            writer.close()
            del self._sockets[conn_id_hex]
            log.info(f"closed target connection for {conn_id_hex}")


class STORMServer:
    """STORM server gateway"""
    
    def __init__(
        self,
        listen_host: str = "0.0.0.0",
        listen_port: int = 53,
        target_host: str = "127.0.0.1",
        target_port: int = 443,
        tunnel_zone: str = "t1.phonexpress.ir",
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.tunnel_zone = tunnel_zone
        
        # Connection management
        self.conn_config = ConnectionConfig()
        self.conn_manager = ConnectionManager(self.conn_config)
        
        # Target connector
        self.target = TargetConnector(target_host, target_port)
        self.dns_server: Optional[STORMDNSServer] = None
        
        self.running = False
    
    async def start(self) -> None:
        """Start DNS listener"""
        log.info(f"STORM server listening on {self.listen_host}:{self.listen_port}/UDP")
        self.running = True
        self.dns_server = STORMDNSServer(
            listen_host=self.listen_host,
            listen_port=self.listen_port,
            tunnel_zone=self.tunnel_zone,
            connection_handler=self._handle_storm_packet_from_dns,
        )
        await self.dns_server.start()
    
    async def handle_dns_query(
        self,
        query_data: bytes,
        client_addr: tuple,
    ) -> Optional[bytes]:
        """
        Handle incoming DNS query.
        Returns DNS response or None.
        """
        try:
            # This is where DNS parsing would happen
            # For now, extract STORM packet from DNS query
            # (Implementation depends on DNS library used)
            
            # Placeholder: assume STORM packet is embedded
            storm_packet = query_data
            
            return await self._process_storm_packet(storm_packet)
        
        except Exception as e:
            log.error(f"query processing error: {e}")
            return None
    
    async def _process_storm_packet(self, packet_data: bytes) -> Optional[bytes]:
        """Process incoming STORM packet"""
        try:
            header, payload = parse_packet(packet_data)
        
        except Exception as e:
            log.error(f"packet parse error: {e}")
            return None
        
        conn_id_hex = header.conn_id.hex()
        
        # Get or create STORM connection
        storm_conn = self.conn_manager.get_connection(header.conn_id)
        if not storm_conn:
            storm_conn = self.conn_manager.create_connection(conn_id=header.conn_id)
            log.info(f"created STORM connection {conn_id_hex}")
            
            # Start background task for this connection
            asyncio.create_task(self._handle_storm_connection(storm_conn))
        
        # Handle incoming packet
        await storm_conn.handle_incoming_packet(packet_data)
        
        # Generate response
        response = await self._generate_response(storm_conn)
        
        return response
    
    async def _handle_storm_connection(self, storm_conn: STORMConnection) -> None:
        """Background task for connection lifecycle"""
        conn_id_hex = storm_conn.conn_id_hex()
        
        try:
            # Connect to target
            target_conn = await self.target.connect(conn_id_hex)
            if not target_conn:
                log.warning(f"could not connect to target for {conn_id_hex}")
                return
            
            reader, writer = target_conn
            
            # Forward data between STORM and target TCP
            await asyncio.gather(
                self._forward_storm_to_target(storm_conn, writer),
                self._forward_target_to_storm(reader, storm_conn),
            )
        
        except Exception as e:
            log.error(f"connection task error: {e}")
        
        finally:
            self.target.close_connection(conn_id_hex)
            self.conn_manager.remove_connection(storm_conn.conn_id)
    
    async def _forward_storm_to_target(
        self,
        storm_conn: STORMConnection,
        target_writer: asyncio.StreamWriter,
    ) -> None:
        """Forward STORM data to target TCP"""
        try:
            while not target_writer.is_closing():
                data = await storm_conn.get_ordered_data(timeout=0.5)
                if data:
                    log.debug(f"forward {len(data)} bytes to target")
                    target_writer.write(data)
                    await target_writer.drain()
        
        except Exception as e:
            log.error(f"forward to target error: {e}")
    
    async def _forward_target_to_storm(
        self,
        target_reader: asyncio.StreamReader,
        storm_conn: STORMConnection,
    ) -> None:
        """Forward target TCP data to STORM"""
        try:
            while not target_reader.at_eof():
                data = await target_reader.read(4096)
                if not data:
                    break
                
                log.debug(f"forward {len(data)} bytes to STORM")
                await storm_conn.send_data(data)
        
        except Exception as e:
            log.error(f"forward from target error: {e}")
    
    async def _generate_response(
        self,
        storm_conn: STORMConnection,
    ) -> Optional[bytes]:
        """Generate DNS response with STORM packet"""
        # Get next outgoing packet
        packet = storm_conn.get_next_outgoing()
        if not packet:
            # Respond with empty/keepalive
            return make_packet(
                conn_id=storm_conn.conn_id,
                flags=PacketFlags.KEEPALIVE_ACK,
                seq_offset=0,
                payload=b"",
            )
        
        return packet
    
    async def _handle_storm_packet_from_dns(
        self,
        packet_data: bytes,
        client_addr: tuple,
    ) -> Optional[bytes]:
        """Adapter for STORMDNSServer connection_handler signature."""
        return await self._process_storm_packet(packet_data)


async def main():
    """Example server usage"""
    parser = argparse.ArgumentParser(description="STORM server gateway")
    parser.add_argument("--listen", default="0.0.0.0:53", help="Listen address host:port")
    parser.add_argument("--target", default="127.0.0.1:8443", help="Target address host:port")
    parser.add_argument("--zone", default="t1.phonexpress.ir", help="Tunnel DNS zone")
    args = parser.parse_args()
    
    listen_host, listen_port = args.listen.rsplit(":", 1)
    target_host, target_port = args.target.rsplit(":", 1)
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    server = STORMServer(
        listen_host=listen_host,
        listen_port=int(listen_port),
        target_host=target_host,
        target_port=int(target_port),
        tunnel_zone=args.zone,
    )
    
    try:
        await server.start()
    except KeyboardInterrupt:
        print("Shutting down...")
        server.running = False


if __name__ == "__main__":
    asyncio.run(main())
