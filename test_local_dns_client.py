"""
STORM DNS Test Client

Sends test STORM packets via DNS queries to local DNS server.
Run storm_dns_server.py first, then run this client.
"""

import asyncio
import logging
import secrets
import dns.message
import dns.name
import dns.rdataclass

from storm_proto import make_packet, PacketFlags, make_frame, FrameType
from storm_dns import DNSTransport

log = logging.getLogger("storm-dns-test-client")


async def run_dns_query():
    """Test sending STORM packet via DNS query"""
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    print("=" * 60)
    print("STORM DNS Test Client")
    print("=" * 60)
    print("\nMake sure storm_dns_server.py is running!")
    print("Server: 127.0.0.1:5353\n")
    
    # Create test STORM packet
    conn_id = secrets.token_bytes(4)
    print(f"📦 Creating test packet...")
    print(f"  conn_id: {conn_id.hex()}")
    
    # Create a frame
    frame = make_frame(
        frame_type=FrameType.DATA,
        frame_flags=0,
        seq_group=0,
        payload=b"Hello from STORM DNS Test Client!",
    )
    
    # Wrap in STORM packet
    packet = make_packet(
        conn_id=conn_id,
        flags=PacketFlags.DATA,
        seq_offset=0,
        payload=frame,
    )
    
    print(f"  packet size: {len(packet)} bytes")
    
    # Create DNS transport
    transport = DNSTransport(zone="t1.phonexpress.ir", qtype="TXT")
    
    # Create DNS query name
    query_name = transport.make_query_name(
        packet,
        session_id=conn_id.hex()[:16],
    )
    
    print(f"\n📋 DNS Query:")
    print(f"  name: {query_name[:80]}...")
    print(f"  type: TXT")
    
    # Build DNS query
    q = dns.message.make_query(
        query_name,
        "TXT",
        dns.rdataclass.IN,
    )
    
    # Add EDNS0
    q.use_edns(edns=0, ednsflags=0, payload=4096)
    
    print(f"  size: {len(q.to_wire())} bytes")
    
    # Send query
    print(f"\n📤 Sending query to 127.0.0.1:5353...")
    
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        
        sock.sendto(q.to_wire(), ("127.0.0.1", 5353))
        
        # Receive response
        response_wire, _ = sock.recvfrom(4096)
        response = dns.message.from_wire(response_wire)
        
        print(f"\n✅ Received response!")
        print(f"  size: {len(response_wire)} bytes")
        print(f"  rcode: {dns.rcode.to_text(response.rcode())}")
        
        # Check for answer
        if response.answer:
            print(f"  answer records: {len(response.answer)}")
            for rrset in response.answer:
                print(f"    - {rrset}")
        else:
            print(f"  (no answer records)")
        
        print("\n✅ DNS query/response cycle successful!")
    
    except socket.timeout:
        print(f"\n❌ Timeout - is the server running?")
    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        sock.close()
    
    print("\n" + "=" * 60)


async def run_multiple_packets():
    """Test sending multiple packets"""
    
    print("\n" + "=" * 60)
    print("Testing Multiple Packets")
    print("=" * 60 + "\n")
    
    transport = DNSTransport(zone="t1.phonexpress.ir")
    
    for i in range(3):
        print(f"Test {i+1}/3...")
        
        # Create unique packet
        conn_id = secrets.token_bytes(4)
        frame = make_frame(
            frame_type=FrameType.DATA,
            frame_flags=0,
            seq_group=i,
            payload=f"Packet {i+1}".encode(),
        )
        packet = make_packet(
            conn_id=conn_id,
            flags=PacketFlags.DATA,
            seq_offset=i,
            payload=frame,
        )
        
        query_name = transport.make_query_name(packet)
        
        q = dns.message.make_query(query_name, "TXT", dns.rdataclass.IN)
        q.use_edns(edns=0, ednsflags=0, payload=4096)
        
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            
            sock.sendto(q.to_wire(), ("127.0.0.1", 5353))
            response_wire, _ = sock.recvfrom(4096)
            
            print(f"  ✅ Response received ({len(response_wire)} bytes)")
            sock.close()
        
        except Exception as e:
            print(f"  ❌ Error: {e}")
        
        await asyncio.sleep(0.1)
    
    print("\n✅ All tests completed!")


async def main():
    """Main entry point"""
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    print("\n🚀 STORM DNS Test Client\n")
    
    print("Tests:")
    print("  1. Single DNS query")
    print("  2. Multiple packets")
    print("  3. Both\n")
    
    choice = input("Select (1/2/3) [default=1]: ").strip() or "1"
    
    if choice in ("1", "3"):
        await run_dns_query()
    
    if choice in ("2", "3"):
        await run_multiple_packets()


if __name__ == "__main__":
    asyncio.run(main())
