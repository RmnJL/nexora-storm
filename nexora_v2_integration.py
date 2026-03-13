"""
STORM - Nexora v2 Integration

Integrates STORM protocol as transport layer for Nexora v2.
Provides carrier-based connection multiplexing with STORM reliability.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, List

from storm_connection import STORMConnection, ConnectionManager, ConnectionConfig
from storm_failover import ResolverSelector
from storm_dns import DNSGateway
from storm_proto import make_frame, FrameType, PacketFlags

log = logging.getLogger("storm-nexora-v2")

# Import Nexora v2 primitives if available
try:
    from nexora.src.nexora_v2 import (
        CarrierManager,
        StreamMux,
        pack_envelope,
        unpack_envelope,
    )
    NEXORA_V2_AVAILABLE = True
except ImportError:
    NEXORA_V2_AVAILABLE = False
    log.warning("Nexora v2 not available - integration disabled")


class CarrierTransport(str, Enum):
    """Transport layer for carrier"""
    STORM = "storm"
    DNS_V1 = "dns_v1"


@dataclass
class CarrierConfig:
    """Configuration for STORM-based carrier"""
    transport: CarrierTransport = CarrierTransport.STORM
    max_carriers: int = 3
    keepalive_interval: float = 10.0
    rto_initial: float = 1.0  # Initial RTO
    rto_max: float = 30.0


class STORMCarrier:
    """
    STORM-based carrier for Nexora v2.
    
    Replaces per-connection DNS sessions with resilient, resumable carriers.
    Multiplexes multiple streams over single STORM connection.
    """
    
    def __init__(
        self,
        carrier_id: int,
        resolvers: List[str],
        config: Optional[CarrierConfig] = None,
    ):
        self.carrier_id = carrier_id
        self.resolvers = resolvers
        self.config = config or CarrierConfig()
        
        # STORM connection (resumable by conn_id)
        self.storm_conn = STORMConnection(
            config=ConnectionConfig(block_size=16),
        )
        
        # Stream multiplexing
        self.stream_map: Dict[int, asyncio.Queue] = {}  # stream_id -> recv queue
        self.stream_lock = asyncio.Lock()
        
        # DNS transport with resolver failover
        self.resolver_selector = ResolverSelector(resolvers)
        self.dns_gateway = DNSGateway(resolvers=resolvers)
        
        # Health metrics
        self.packets_sent = 0
        self.packets_received = 0
        self.active = False
    
    async def start(self) -> None:
        """Start carrier"""
        log.info(f"Carrier {self.carrier_id} starting with STORM transport")
        self.active = True
        
        # Start keepalive task
        asyncio.create_task(self._keepalive_task())
        
        # Start packet transmission task
        asyncio.create_task(self._transmit_task())
    
    async def multiplex_stream(
        self,
        stream_id: int,
        data: bytes,
    ) -> None:
        """Send data on a stream (multiplexed over carrier)"""
        
        if not self.active:
            raise RuntimeError("Carrier not active")
        
        # Queue data for transmission
        await self.storm_conn.send_data(data)
    
    async def receive_stream(
        self,
        stream_id: int,
        timeout: float = 30.0,
    ) -> Optional[bytes]:
        """Receive data on a stream"""
        
        async with self.stream_lock:
            if stream_id not in self.stream_map:
                self.stream_map[stream_id] = asyncio.Queue()
        
        queue = self.stream_map[stream_id]
        
        try:
            data = await asyncio.wait_for(queue.get(), timeout=timeout)
            return data
        except asyncio.TimeoutError:
            return None
    
    async def _keepalive_task(self) -> None:
        """Keepalive ping task"""
        while self.active:
            await asyncio.sleep(self.config.keepalive_interval)
            
            try:
                # Send keepalive frame
                keepalive_frame = make_frame(
                    frame_type=FrameType.DATA,
                    frame_flags=0,
                    seq_group=0,
                    payload=b"KEEPALIVE",
                )
                
                # Send via STORM
                primary = self.resolver_selector.select_primary()
                _, latency = await self.dns_gateway.send_to_specific(
                    keepalive_frame,
                    primary,
                    session_id=self.storm_conn.conn_id_hex(),
                    timeout=3.0,
                )
                
                if latency > 0:
                    self.resolver_selector.report_success(primary, latency)
                
                log.debug(f"Carrier {self.carrier_id} keepalive: {latency*1000:.2f}ms")
            
            except Exception as e:
                log.warning(f"Keepalive error: {e}")
    
    async def _transmit_task(self) -> None:
        """Packet transmission task"""
        while self.active:
            try:
                # Get next outgoing packet
                packet = self.storm_conn.get_next_outgoing()
                
                if not packet:
                    await asyncio.sleep(0.01)
                    continue
                
                # Select resolver
                primary, secondary = self.resolver_selector.select_pair()
                
                # Send via DNS
                resp, resolver, latency = await self.dns_gateway.send_to_any(
                    packet,
                    session_id=self.storm_conn.conn_id_hex(),
                    timeout=3.0,
                )
                
                self.packets_sent += 1
                
                if resp is not None:
                    self.packets_received += 1
                    self.resolver_selector.report_success(resolver, latency)
                    
                    # Process response
                    await self.storm_conn.handle_incoming_packet(resp)
                else:
                    self.resolver_selector.report_failure(resolver, is_timeout=True)
            
            except Exception as e:
                log.debug(f"Transmission error: {e}")
            
            await asyncio.sleep(0.01)
    
    async def close(self) -> None:
        """Close carrier"""
        log.info(f"Carrier {self.carrier_id} closing")
        self.active = False
        await self.storm_conn.close()
    
    def get_stats(self) -> dict:
        """Get carrier statistics"""
        return {
            "carrier_id": self.carrier_id,
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "active": self.active,
            "storm_conn": self.storm_conn.conn_id_hex(),
        }


class STORMNexoraV2Gateway:
    """
    Gateway combining STORM carriers with Nexora v2 multiplexing.
    
    Replaces Nexora v1's per-connection complexity with:
    - Resumable STORM carriers (transport)
    - Stream multiplexing (Nexora v2)
    - Automatic failover
    """
    
    def __init__(
        self,
        resolvers: List[str],
        zone: str = "t1.phonexpress.ir",
        config: Optional[CarrierConfig] = None,
    ):
        self.resolvers = resolvers
        self.zone = zone
        self.config = config or CarrierConfig()
        
        # Carrier management
        self.carriers: Dict[int, STORMCarrier] = {}
        self.next_carrier_id = 1
        self.carrier_lock = asyncio.Lock()
        
        # Optionally use Nexora v2 primitives if available
        if NEXORA_V2_AVAILABLE:
            self.nexora_carriers = CarrierManager(
                resolvers,
                max_carriers=self.config.max_carriers,
            )
            self.stream_mux = StreamMux()
        else:
            self.nexora_carriers = None
            self.stream_mux = None
    
    async def get_or_create_carrier(self) -> STORMCarrier:
        """Get available carrier or create new one"""
        
        async with self.carrier_lock:
            # Return first active carrier if available
            for carrier in self.carriers.values():
                if carrier.active:
                    return carrier
            
            # Create new carrier
            carrier_id = self.next_carrier_id
            self.next_carrier_id += 1
            
            carrier = STORMCarrier(
                carrier_id=carrier_id,
                resolvers=self.resolvers,
                config=self.config,
            )
            
            await carrier.start()
            self.carriers[carrier_id] = carrier
            
            log.info(f"Created new carrier {carrier_id}")
            return carrier
    
    async def send_data(self, stream_id: int, data: bytes) -> None:
        """Send data on stream"""
        carrier = await self.get_or_create_carrier()
        await carrier.multiplex_stream(stream_id, data)
    
    async def receive_data(self, stream_id: int, timeout: float = 30.0) -> Optional[bytes]:
        """Receive data on stream"""
        carrier = await self.get_or_create_carrier()
        return await carrier.receive_stream(stream_id, timeout)
    
    async def close(self) -> None:
        """Close all carriers"""
        for carrier in self.carriers.values():
            await carrier.close()
    
    def get_stats(self) -> dict:
        """Get gateway statistics"""
        return {
            "carriers": len(self.carriers),
            "carrier_stats": [
                carrier.get_stats() for carrier in self.carriers.values()
            ],
            "nexora_v2_available": NEXORA_V2_AVAILABLE,
        }


async def demo_nexora_v2_integration():
    """Demo STORM-Nexora v2 integration"""
    
    print("=" * 60)
    print("STORM - Nexora v2 Integration Demo")
    print("=" * 60)
    
    if not NEXORA_V2_AVAILABLE:
        print("\n⚠️  Nexora v2 not available for full integration")
        print("    STORM transport layer can still work independently")
    
    resolvers = [
        "8.8.8.8",
        "1.1.1.1",
    ]
    
    print(f"\n📍 Resolvers: {', '.join(resolvers)}")
    print("🚀 Starting STORM-Nexora v2 gateway...")
    
    gateway = STORMNexoraV2Gateway(resolvers=resolvers)
    
    try:
        # Simulate 3 concurrent streams
        print("\n📊 Simulating 3 concurrent streams...")
        
        async def stream_task(stream_id: int):
            """Simulate stream communication"""
            carrier = await gateway.get_or_create_carrier()
            
            for i in range(3):
                data = f"Stream {stream_id} message {i+1}".encode()
                print(f"  Stream {stream_id}: sending {len(data)} bytes")
                await carrier.multiplex_stream(stream_id, data)
                await asyncio.sleep(0.5)
        
        # Run 3 streams
        await asyncio.gather(
            stream_task(1),
            stream_task(2),
            stream_task(3),
        )
        
        # Print stats
        await asyncio.sleep(1)
        stats = gateway.get_stats()
        
        print(f"\n📈 Statistics:")
        print(f"  Carriers: {stats['carriers']}")
        for cs in stats['carrier_stats']:
            print(f"    - Carrier {cs['carrier_id']}: "
                  f"{cs['packets_sent']} sent, "
                  f"{cs['packets_received']} received")
    
    finally:
        await gateway.close()
    
    print("\n✅ Demo complete!")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    asyncio.run(demo_nexora_v2_integration())
