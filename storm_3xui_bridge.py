#!/usr/bin/env python3
"""
STORM + 3x-UI Bridge

Bridges 3x-UI VLESS connections through STORM DNS tunnel.
Users manage via 3x-UI panel, data flows through STORM.

Architecture:
  3x-UI (UUID mgmt) → VLESS :443 → STORM Bridge → DNS Query → Outside
"""

import asyncio
import logging
import ssl
import struct
import uuid as uuid_module
from typing import Dict, Optional, Set
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from nexora_v2_integration import STORMNexoraV2Gateway, CarrierConfig
from storm_encryption import STORMCrypto

log = logging.getLogger("storm-3xui-bridge")


class VLESSHandshake:
    """VLESS Protocol handshake parser"""
    
    VLESS_VERSION = 0
    
    @staticmethod
    def parse_request(data: bytes) -> Optional[Dict]:
        """
        Parse VLESS request header.
        
        Format:
          [1 byte] version
          [16 bytes] UUID
          [1 byte] addons length
          ...addons...
          [1 byte] command (1=TCP, 2=UDP, 3=MUX)
          [2 bytes] port
          [1 byte] atyp (1=IPv4, 3=domain, 4=IPv6)
          ...address...
        """
        
        if len(data) < 1:
            return None
        
        version = data[0]
        if version != VLESSHandshake.VLESS_VERSION:
            log.error(f"Invalid VLESS version: {version}")
            return None
        
        if len(data) < 18:  # version + UUID
            return None
        
        user_uuid = str(uuid_module.UUID(bytes=data[1:17]))
        addons_len = data[17]
        
        # Simple parsing (skip addons for now)
        offset = 18 + addons_len
        
        if offset + 4 > len(data):
            return None
        
        command = data[offset]
        port = struct.unpack(">H", data[offset+1:offset+3])[0]
        
        return {
            "uuid": user_uuid,
            "command": command,
            "port": port,
            "offset": offset + 3,
        }
    
    @staticmethod
    def create_response() -> bytes:
        """Create VLESS response header"""
        return bytes([0])  # Version 0, no addons


class STORM3XUIBridge:
    """
    STORM Bridge for 3x-UI
    
    Listens on :443 for VLESS connections.
    Forwards through STORM tunnel.
    Manages per-user streams.
    """
    
    def __init__(
        self,
        storm_gateway: STORMNexoraV2Gateway,
        valid_uuids: Optional[Set[str]] = None,
        listen_host: str = "0.0.0.0",
        listen_port: int = 443,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self.gateway = storm_gateway
        self.valid_uuids = valid_uuids or set()
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.ssl_context = ssl_context
        
        # Connection tracking
        self.active_connections: Dict[str, Dict] = {}
        self.bytes_in: Dict[str, int] = {}
        self.bytes_out: Dict[str, int] = {}
    
    def add_user(self, user_uuid: str):
        """Add valid UUID (called from 3x-UI config)"""
        self.valid_uuids.add(user_uuid)
        log.info(f"Added user: {user_uuid}")
    
    def remove_user(self, user_uuid: str):
        """Remove user (called when disabled in 3x-UI)"""
        self.valid_uuids.discard(user_uuid)
        log.info(f"Removed user: {user_uuid}")
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming VLESS connection"""
        
        peer = writer.get_extra_info('peername')
        log.debug(f"New connection from {peer}")
        
        try:
            # Step 1: Read VLESS handshake
            handshake_data = await reader.readexactly(58)  # Min VLESS header
            
            # Parse request
            request = VLESSHandshake.parse_request(handshake_data)
            if not request:
                log.warning(f"Invalid handshake from {peer}")
                writer.close()
                return
            
            user_uuid = request["uuid"]
            
            # Step 2: Validate UUID
            if user_uuid not in self.valid_uuids:
                log.warning(f"Invalid UUID: {user_uuid} from {peer}")
                writer.close()
                return
            
            log.info(f"Authenticated user: {user_uuid} from {peer}")
            
            # Step 3: Send response header
            response = VLESSHandshake.create_response()
            writer.write(response)
            await writer.drain()
            
            # Step 4: Stream through STORM tunnel
            stream_id = hash(user_uuid) % 10000
            
            self.active_connections[user_uuid] = {
                "peer": peer,
                "stream_id": stream_id,
                "connected_at": asyncio.get_event_loop().time(),
            }
            
            self.bytes_in[user_uuid] = 0
            self.bytes_out[user_uuid] = 0
            
            await self.bridge_stream(reader, writer, user_uuid, stream_id)
        
        except asyncio.IncompleteReadError:
            log.debug(f"Connection closed: {peer}")
        except Exception as e:
            log.error(f"Error handling {peer}: {e}")
        finally:
            writer.close()
            if peer:
                self.active_connections.pop(str(peer), None)
    
    async def bridge_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        user_uuid: str,
        stream_id: int,
    ):
        """Bridge VLESS stream ↔ STORM tunnel"""
        
        try:
            # Bidirectional forwarding
            recv_task = asyncio.create_task(self._receive_from_client(reader, stream_id, user_uuid))
            send_task = asyncio.create_task(self._send_to_client(reader, writer, stream_id, user_uuid))
            
            await asyncio.gather(recv_task, send_task)
        
        except Exception as e:
            log.error(f"Bridge error for {user_uuid}: {e}")
    
    async def _receive_from_client(
        self,
        reader: asyncio.StreamReader,
        stream_id: int,
        user_uuid: str,
    ):
        """Read from VLESS client, send through STORM tunnel"""
        
        try:
            while True:
                data = await reader.read(4096)
                
                if not data:
                    break
                
                # Track bytes
                self.bytes_in[user_uuid] = self.bytes_in.get(user_uuid, 0) + len(data)
                
                # Send through STORM
                await self.gateway.send_data(stream_id=stream_id, data=data)
                
                log.debug(f"User {user_uuid}: sent {len(data)} bytes via STORM")
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Receive error for {user_uuid}: {e}")
    
    async def _send_to_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        stream_id: int,
        user_uuid: str,
    ):
        """Read from STORM tunnel, send to VLESS client"""
        
        try:
            while True:
                # Receive from STORM tunnel
                data = await self.gateway.receive_data(
                    stream_id=stream_id,
                    timeout=30.0,
                )
                
                if not data:
                    log.debug(f"STORM timeout for {user_uuid}")
                    break
                
                # Track bytes
                self.bytes_out[user_uuid] = self.bytes_out.get(user_uuid, 0) + len(data)
                
                # Send to client
                writer.write(data)
                await writer.drain()
                
                log.debug(f"User {user_uuid}: received {len(data)} bytes from STORM")
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Send error for {user_uuid}: {e}")
    
    async def start(self):
        """Start bridge server"""
        
        log.info(f"Starting STORM Bridge on {self.listen_host}:{self.listen_port}")
        
        server = await asyncio.start_server(
            self.handle_client,
            self.listen_host,
            self.listen_port,
            ssl=self.ssl_context,
        )
        
        async with server:
            log.info("🟢 STORM Bridge listening")
            await server.serve_forever()
    
    def get_stats(self) -> Dict:
        """Get bridge statistics"""
        
        total_in = sum(self.bytes_in.values())
        total_out = sum(self.bytes_out.values())
        
        return {
            "active_users": len(self.active_connections),
            "total_bytes_in": total_in,
            "total_bytes_out": total_out,
            "valid_uuids": len(self.valid_uuids),
            "connections": [
                {
                    "uuid": uuid,
                    "bytes_in": self.bytes_in.get(uuid, 0),
                    "bytes_out": self.bytes_out.get(uuid, 0),
                }
                for uuid in self.active_connections.keys()
            ],
        }


async def main():
    """Main entry point"""
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("/var/log/storm_bridge.log"),
        ],
    )
    
    # Get configuration
    resolvers = os.getenv("RESOLVERS", "8.8.8.8,1.1.1.1,9.9.9.9").split(",")
    listen_port = int(os.getenv("LISTEN_PORT", 443))
    
    # Demo UUIDs (in production, read from 3x-UI config)
    demo_uuids = {
        "550e8400-e29b-41d4-a716-446655440000",  # Demo user 1
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # Demo user 2
    }
    
    # Create STORM gateway
    gateway = STORMNexoraV2Gateway(
        resolvers=resolvers,
        config=CarrierConfig(max_carriers=5),
    )
    
    # Create bridge
    bridge = STORM3XUIBridge(
        storm_gateway=gateway,
        valid_uuids=demo_uuids,
        listen_port=listen_port,
    )
    
    # Monitoring task
    async def monitor():
        """Print statistics periodically"""
        while True:
            await asyncio.sleep(30)
            stats = bridge.get_stats()
            log.info(f"Stats: {stats['active_users']} users, "
                    f"{stats['total_bytes_in']//1024} KB in, "
                    f"{stats['total_bytes_out']//1024} KB out")
    
    # Start monitoring
    asyncio.create_task(monitor())
    
    # Start bridge
    try:
        await bridge.start()
    except KeyboardInterrupt:
        log.info("Bridge stopped")
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
