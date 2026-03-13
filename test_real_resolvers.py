"""
STORM Real Resolver Testing

Tests STORM protocol against real public DNS resolvers.
Monitors performance and reliability metrics.
"""

import asyncio
import logging
import secrets
import time
from typing import List, Dict, Optional
from dataclasses import dataclass

from storm_proto import make_packet, PacketFlags, make_frame, FrameType
from storm_dns import DNSTransport
from storm_failover import ResolverSelector
import dns.message
import dns.rdataclass
import socket

log = logging.getLogger("storm-real-resolver-test")

# Real public resolvers
REAL_RESOLVERS = [
    "8.8.8.8",           # Google
    "1.1.1.1",           # Cloudflare
    "9.9.9.9",           # Quad9
    # "185.49.84.2",     # Custom (if available)
]


@dataclass
class ResolverTestResult:
    """Single test result"""
    resolver: str
    success: bool
    latency_ms: float
    error: Optional[str] = None


@dataclass
class ResolverStats:
    """Statistics for a resolver"""
    resolver: str
    total: int = 0
    success: int = 0
    failures: int = 0
    timeouts: int = 0
    latencies: List[float] = None
    
    def __post_init__(self):
        if self.latencies is None:
            self.latencies = []
    
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.success / self.total
    
    def avg_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)
    
    def min_latency(self) -> float:
        return min(self.latencies) if self.latencies else 0.0
    
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else 0.0


class RealResolverTester:
    """Test STORM against real resolvers"""
    
    def __init__(
        self,
        resolvers: Optional[List[str]] = None,
        zone: str = "t1.phonexpress.ir",
    ):
        self.resolvers = resolvers or REAL_RESOLVERS
        self.zone = zone
        self.transport = DNSTransport(zone=zone)
        
        self.stats: Dict[str, ResolverStats] = {
            r: ResolverStats(resolver=r) for r in self.resolvers
        }
        
        self.selector = ResolverSelector(self.resolvers)
    
    async def test_single_resolver(
        self,
        resolver: str,
        packet_size: int = 100,
        timeout: float = 3.0,
    ) -> ResolverTestResult:
        """Test single resolver"""
        
        try:
            # Create test packet
            conn_id = secrets.token_bytes(4)
            frame = make_frame(
                frame_type=FrameType.DATA,
                frame_flags=0,
                seq_group=0,
                payload=b"X" * packet_size,
            )
            packet = make_packet(
                conn_id=conn_id,
                flags=PacketFlags.DATA,
                seq_offset=0,
                payload=frame,
            )
            
            # Create DNS query
            query_name = self.transport.make_query_name(packet)
            q = dns.message.make_query(query_name, "TXT", dns.rdataclass.IN)
            q.use_edns(edns=0, ednsflags=0, payload=4096)
            
            # Send query
            start = time.time()
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            
            try:
                sock.sendto(q.to_wire(), (resolver, 53))
                response_wire, _ = sock.recvfrom(4096)
                
                latency = (time.time() - start) * 1000  # ms
                
                # Update stats
                self.stats[resolver].total += 1
                self.stats[resolver].success += 1
                self.stats[resolver].latencies.append(latency)
                self.selector.report_success(resolver, latency)
                
                return ResolverTestResult(
                    resolver=resolver,
                    success=True,
                    latency_ms=latency,
                )
            
            except socket.timeout:
                self.stats[resolver].total += 1
                self.stats[resolver].failures += 1
                self.stats[resolver].timeouts += 1
                self.selector.report_failure(resolver, is_timeout=True)
                
                return ResolverTestResult(
                    resolver=resolver,
                    success=False,
                    latency_ms=timeout * 1000,
                    error="timeout",
                )
            
            finally:
                sock.close()
        
        except Exception as e:
            self.stats[resolver].total += 1
            self.stats[resolver].failures += 1
            self.selector.report_failure(resolver, is_timeout=False)
            
            return ResolverTestResult(
                resolver=resolver,
                success=False,
                latency_ms=0.0,
                error=str(e),
            )
    
    async def run_test_suite(
        self,
        queries_per_resolver: int = 10,
        packet_sizes: Optional[List[int]] = None,
    ):
        """Run comprehensive test suite"""
        
        if not packet_sizes:
            packet_sizes = [50, 100, 200, 500]
        
        print("=" * 70)
        print("STORM Real Resolver Testing")
        print("=" * 70)
        
        print(f"\n📍 Resolvers: {len(self.resolvers)}")
        for r in self.resolvers:
            print(f"   - {r}")
        
        print(f"\n🔧 Test Configuration:")
        print(f"   Queries per resolver: {queries_per_resolver}")
        print(f"   Packet sizes: {packet_sizes}")
        print(f"   Zone: {self.zone}")
        
        print(f"\n🚀 Starting tests...\n")
        
        total_tests = len(self.resolvers) * queries_per_resolver * len(packet_sizes)
        tests_done = 0
        
        for packet_size in packet_sizes:
            print(f"\n📦 Packet Size: {packet_size} bytes")
            print("-" * 70)
            
            for query_num in range(queries_per_resolver):
                for resolver in self.resolvers:
                    result = await self.test_single_resolver(
                        resolver,
                        packet_size=packet_size,
                    )
                    
                    tests_done += 1
                    progress = (tests_done / total_tests) * 100
                    
                    status = "✅" if result.success else "❌"
                    print(
                        f"{status} {result.resolver:15} "
                        f"Query {query_num+1}/{queries_per_resolver} "
                        f"Size {packet_size} "
                        f"Lat {result.latency_ms:6.2f}ms "
                        f"[{progress:5.1f}%]",
                        end="\r"
                    )
                    
                    await asyncio.sleep(0.05)  # Rate limiting
        
        print("\n\n" + "=" * 70)
        self._print_summary()
    
    def _print_summary(self):
        """Print test summary"""
        print("SUMMARY")
        print("=" * 70)
        
        print("\nPer-Resolver Statistics:")
        print("-" * 70)
        print(f"{'Resolver':<15} {'Total':<8} {'Success':<8} {'Fail':<8} {'Rate':<8} {'Avg Lat':<10}")
        print("-" * 70)
        
        for resolver in self.resolvers:
            stats = self.stats[resolver]
            print(
                f"{stats.resolver:<15} "
                f"{stats.total:<8} "
                f"{stats.success:<8} "
                f"{stats.failures:<8} "
                f"{stats.success_rate()*100:>6.1f}% "
                f"{stats.avg_latency():>8.2f}ms"
            )
        
        print("\nLatency Distribution:")
        print("-" * 70)
        
        for resolver in self.resolvers:
            stats = self.stats[resolver]
            if not stats.latencies:
                continue
            
            lats = sorted(stats.latencies)
            print(
                f"{resolver:<15} "
                f"Min {min(lats):>7.2f}ms "
                f"P50 {lats[len(lats)//2]:>7.2f}ms "
                f"P95 {lats[int(len(lats)*0.95)]:>7.2f}ms "
                f"Max {max(lats):>7.2f}ms"
            )
        
        print("\nReliability Assessment:")
        print("-" * 70)
        
        for resolver in self.resolvers:
            stats = self.stats[resolver]
            rate = stats.success_rate() * 100
            
            if rate >= 95:
                assessment = "✅ Excellent"
            elif rate >= 80:
                assessment = "⚠️  Good"
            elif rate >= 50:
                assessment = "⚠️  Fair"
            else:
                assessment = "❌ Poor"
            
            print(f"{resolver:<15} {assessment} ({rate:.1f}%)")
        
        # Best resolver
        best = max(self.resolvers, key=lambda r: self.stats[r].success_rate())
        print(f"\n🏆 Best Resolver: {best} ({self.stats[best].success_rate()*100:.1f}% success)")
        
        print("\n" + "=" * 70)
    
    async def monitor_health(self, duration_seconds: int = 60, interval: int = 10):
        """Monitor resolver health over time"""
        
        print("=" * 70)
        print(f"STORM Resolver Health Monitoring ({duration_seconds}s)")
        print("=" * 70)
        print()
        
        start_time = time.time()
        
        while time.time() - start_time < duration_seconds:
            print(f"\n⏱️  Time: {int(time.time() - start_time)}s/{duration_seconds}s")
            print("-" * 70)
            
            # Test each resolver
            for resolver in self.resolvers:
                result = await self.test_single_resolver(resolver)
                status = "✅" if result.success else "❌"
                print(f"{status} {resolver:<15} Lat {result.latency_ms:>7.2f}ms")
            
            await asyncio.sleep(interval)
        
        print("\n" + "=" * 70)
        self._print_summary()


async def main():
    """Main entry point"""
    
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s: %(message)s"
    )
    
    print("\n🌍 STORM Real Resolver Testing\n")
    
    print("Options:")
    print("  1. Quick test (5 queries per resolver)")
    print("  2. Full test (20 queries, multiple packet sizes)")
    print("  3. Health monitoring (60 seconds, 10s intervals)")
    print()
    
    choice = input("Select (1/2/3) [default=1]: ").strip() or "1"
    
    tester = RealResolverTester(
        resolvers=REAL_RESOLVERS,
        zone="t1.phonexpress.ir",
    )
    
    if choice == "1":
        await tester.run_test_suite(queries_per_resolver=5, packet_sizes=[100])
    elif choice == "2":
        await tester.run_test_suite(queries_per_resolver=20, packet_sizes=[50, 100, 200, 500])
    elif choice == "3":
        await tester.monitor_health(duration_seconds=60, interval=10)
    
    print("\n✅ Testing complete!")


if __name__ == "__main__":
    asyncio.run(main())
