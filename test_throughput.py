"""
STORM Performance Test - Throughput measurement

Tests STORM DNS tunnel throughput end-to-end.
"""

import asyncio
import logging
import secrets
import time
from storm_proto import make_packet, PacketFlags, make_frame, FrameType
from storm_dns import DNSTransport
import dns.message
import dns.rdataclass
import socket

log = logging.getLogger("storm-perf-test")


class PerformanceTest:
    """Measure STORM protocol performance"""
    
    def __init__(self, resolver_ip: str = "127.0.0.1", resolver_port: int = 5353):
        self.resolver_ip = resolver_ip
        self.resolver_port = resolver_port
        self.transport = DNSTransport(zone="t1.phonexpress.ir")
        
        self.packets_sent = 0
        self.packets_received = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.errors = 0
        self.latencies = []
    
    async def run_throughput_test(self, duration_seconds: int = 10, packet_count: int = None):
        """Run throughput test for N seconds or N packets"""
        
        print("=" * 60)
        print("STORM Performance Test - Throughput")
        print("=" * 60)
        print(f"\nTarget: {self.resolver_ip}:{self.resolver_port}")
        print(f"Duration: {duration_seconds}s" if not packet_count else f"Packets: {packet_count}")
        print()
        
        start_time = time.time()
        loop = asyncio.get_event_loop()
        
        try:
            packet_num = 0
            while True:
                if packet_count and packet_num >= packet_count:
                    break
                if time.time() - start_time > duration_seconds:
                    break
                
                # Create test packet
                conn_id = secrets.token_bytes(4)
                frame = make_frame(
                    frame_type=FrameType.DATA,
                    frame_flags=0,
                    seq_group=packet_num % 256,
                    payload=b"X" * 100,  # 100 byte payload
                )
                packet = make_packet(
                    conn_id=conn_id,
                    flags=PacketFlags.DATA,
                    seq_offset=packet_num % 256,
                    payload=frame,
                )
                
                # Send query
                query_name = self.transport.make_query_name(packet)
                q = dns.message.make_query(query_name, "TXT", dns.rdataclass.IN)
                q.use_edns(edns=0, ednsflags=0, payload=4096)
                
                query_wire = q.to_wire()
                self.bytes_sent += len(query_wire)
                
                # Measure round-trip
                elapsed = await self._send_query(query_wire)
                
                if elapsed is not None:
                    self.packets_received += 1
                    self.bytes_received += len(query_wire)
                    self.latencies.append(elapsed * 1000)  # ms
                    print(f"Packet {packet_num+1}: {elapsed*1000:.2f}ms", end="\r")
                else:
                    self.errors += 1
                
                self.packets_sent += 1
                packet_num += 1
                
                await asyncio.sleep(0.01)  # Small delay between packets
        
        except KeyboardInterrupt:
            print("\n\nStopped by user")
        
        elapsed_time = time.time() - start_time
        
        # Print results
        self._print_results(elapsed_time)
    
    async def _send_query(self, query_wire: bytes) -> float:
        """Send DNS query and measure RTT"""
        start = time.time()
        
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            
            sock.sendto(query_wire, (self.resolver_ip, self.resolver_port))
            response_wire, _ = sock.recvfrom(4096)
            
            elapsed = time.time() - start
            sock.close()
            
            return elapsed
        
        except socket.timeout:
            return None
        except Exception as e:
            log.warning(f"Query error: {e}")
            return None
    
    def _print_results(self, elapsed_time: float):
        """Print test results"""
        print("\n\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60)
        
        print(f"\nTest Duration: {elapsed_time:.2f} seconds")
        print(f"Packets Sent: {self.packets_sent}")
        print(f"Packets Received: {self.packets_received}")
        print(f"Errors: {self.errors}")
        
        if self.packets_received > 0:
            success_rate = (self.packets_received / self.packets_sent) * 100
            print(f"Success Rate: {success_rate:.1f}%")
        
        print(f"\nData Sent: {self.bytes_sent / 1024:.2f} KB")
        print(f"Throughput: {self.bytes_sent / 1024 / elapsed_time:.2f} KB/s")
        print(f"Throughput: {self.bytes_sent * 8 / 1024 / 1024 / elapsed_time:.3f} Mbps")
        
        if self.latencies:
            latencies = sorted(self.latencies)
            print(f"\nLatency:")
            print(f"  Min: {min(latencies):.2f} ms")
            print(f"  Avg: {sum(latencies) / len(latencies):.2f} ms")
            print(f"  Max: {max(latencies):.2f} ms")
            print(f"  P50: {latencies[len(latencies)//2]:.2f} ms")
            print(f"  P95: {latencies[int(len(latencies)*0.95)]:.2f} ms")
            print(f"  P99: {latencies[int(len(latencies)*0.99)]:.2f} ms")
        
        print("\n" + "=" * 60)


async def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    print("\n🚀 STORM Performance Tester\n")
    
    print("Make sure storm_dns_server.py is running!")
    print("  python storm_dns_server.py\n")
    
    # Test parameters
    duration = 10  # seconds
    packet_count = None  # or set to specific number
    
    tester = PerformanceTest(
        resolver_ip="127.0.0.1",
        resolver_port=5353,
    )
    
    await tester.run_throughput_test(
        duration_seconds=duration,
        packet_count=packet_count,
    )


if __name__ == "__main__":
    asyncio.run(main())
