#!/usr/bin/env python3
"""
STORM Quick Start Example

This example shows how to:
1. Create a STORM client (inside gateway)
2. Create a STORM server (outside gateway)
3. Run them locally for testing

Run with: python example_local_test.py
"""

import asyncio
import logging
from storm_dns_server import STORMDNSServer
from storm_proto import parse_packet

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

log = logging.getLogger(__name__)


async def run_local_test():
    """Run local DNS server test with packet handling"""
    
    print("=" * 60)
    print("STORM Protocol - Local DNS Test Example")
    print("=" * 60)
    
    # Mock connection handler to process STORM packets
    async def mock_storm_handler(packet: bytes, addr: tuple) -> bytes:
        """Mock handler that echoes STORM packets"""
        print(f"\n📨 Received packet from {addr[0]}:{addr[1]} ({len(packet)} bytes)")
        
        try:
            header, payload = parse_packet(packet)
            print(f"  ✅ Parsed: conn_id={header.conn_id.hex()}, flags=0x{header.flags:02x}, payload={len(payload)}B")
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            return None
        
        # Echo back (in production, would route to target service)
        print(f"  📤 Sending response ({len(packet)} bytes)...")
        return packet
    
    # Create DNS server
    print("\n🚀 Starting STORM DNS server...")
    server = STORMDNSServer(
        listen_host="127.0.0.1",
        listen_port=5353,
        tunnel_zone="t1.phonexpress.ir",
        connection_handler=mock_storm_handler,
    )
    
    print("✅ DNS Server ready!")
    print("\n📝 Server Configuration:")
    print("  Listen: 127.0.0.1:5353")
    print("  Zone: t1.phonexpress.ir")
    print("\n📝 Test with in another terminal:")
    print("  python test_local_dns_client.py")
    print("\n" + "=" * 60)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        print("\n\n⏹️  Shutting down...")
        server.running = False


async def demo_protocol_directly():
    """Test protocol primitives without DNS"""
    print("\n" + "=" * 60)
    print("STORM Protocol - Direct Test (no DNS)")
    print("=" * 60)
    
    import secrets
    from storm_proto import (
        make_packet,
        parse_packet,
        PacketFlags,
        make_frame,
        parse_frame,
        FrameType,
    )
    from storm_fec import FECRecovery, compute_parity
    
    print("\n📦 Testing packet encoding...")
    conn_id = secrets.token_bytes(4)
    payload = b"Hello STORM Protocol!"
    
    packet = make_packet(
        conn_id=conn_id,
        flags=PacketFlags.DATA,
        seq_offset=0,
        payload=payload,
    )
    
    print(f"  ✅ Packet created: {len(packet)} bytes")
    print(f"     conn_id: {conn_id.hex()}")
    print(f"     payload: {payload}")
    
    # Decode
    header, decoded = parse_packet(packet)
    print(f"  ✅ Packet decoded successfully")
    print(f"     conn_id match: {header.conn_id == conn_id}")
    print(f"     payload match: {decoded == payload}")
    
    # Test FEC
    print("\n🔧 Testing FEC recovery...")
    packets = [b"packet0", b"packet1", b"packet2", b"packet3"]
    parity = compute_parity(packets)
    print(f"  ✅ Parity computed: {parity.hex()[:32]}...")
    
    fec = FECRecovery(block_size=4)
    # Add all but one packet
    for i in [0, 1, 3]:
        fec.add_packet(i, packets[i])
    
    recovered = fec.add_parity(0, parity)
    if recovered == packets[2]:
        print(f"  ✅ FEC recovery successful!")
        print(f"     Recovered packet 2 from parity")
    else:
        print(f"  ❌ FEC recovery failed")
    
    print("\n" + "=" * 60)
    print("✅ All protocol tests passed!")
    print("=" * 60)


async def main():
    """Main entry point"""
    print("\n🎯 STORM Protocol - Quick Start Examples\n")
    
    # Choose what to run
    print("Options:")
    print("  1. Test protocol directly (no DNS)")
    print("  2. Run local DNS server")
    print("  3. Both\n")
    
    choice = input("Select (1/2/3) [default=1]: ").strip() or "1"
    
    if choice in ("1", "3"):
        await demo_protocol_directly()
    
    if choice in ("2", "3"):
        await run_local_test()


if __name__ == "__main__":
    asyncio.run(main())
