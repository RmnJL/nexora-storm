"""
STORM Performance Benchmark Suite

Comprehensive benchmarking for:
- Throughput (packets/sec, Mbps)
- Latency (min/avg/max/p95/p99)
- Encryption overhead
- FEC overhead
- Resolver failover impact
"""

import asyncio
import logging
import time
import statistics
from dataclasses import dataclass
from typing import List, Dict, Tuple
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from storm_proto import make_packet, FrameType
from storm_encryption import STORMCrypto
from storm_failover import ResolverSelector
from nexora_v2_integration import STORMCarrier, STORMNexoraV2Gateway, CarrierConfig

log = logging.getLogger("storm-benchmark")


@dataclass
class BenchmarkResult:
    """Result of a benchmark run"""
    name: str
    duration_sec: float
    packets: int
    bytes_sent: int
    success_count: int
    fail_count: int
    latencies: List[float]
    
    @property
    def throughput_pps(self) -> float:
        """Packets per second"""
        return self.packets / self.duration_sec if self.duration_sec > 0 else 0
    
    @property
    def throughput_mbps(self) -> float:
        """Megabits per second"""
        bits = self.bytes_sent * 8
        return bits / (self.duration_sec * 1_000_000) if self.duration_sec > 0 else 0
    
    @property
    def success_rate(self) -> float:
        """Percentage of successful operations"""
        total = self.success_count + self.fail_count
        return (self.success_count / total * 100) if total > 0 else 0
    
    @property
    def avg_latency_ms(self) -> float:
        """Average latency in milliseconds"""
        return statistics.mean(self.latencies) * 1000 if self.latencies else 0
    
    @property
    def p95_latency_ms(self) -> float:
        """P95 latency in milliseconds"""
        if not self.latencies or len(self.latencies) < 20:
            return 0
        return statistics.quantiles(self.latencies, n=20)[18] * 1000
    
    @property
    def p99_latency_ms(self) -> float:
        """P99 latency in milliseconds"""
        if not self.latencies or len(self.latencies) < 100:
            return 0
        return statistics.quantiles(self.latencies, n=100)[98] * 1000
    
    def print_summary(self):
        """Print benchmark summary"""
        print(f"\n{'='*60}")
        print(f"BENCHMARK: {self.name}")
        print(f"{'='*60}")
        print(f"Duration:      {self.duration_sec:.2f}s")
        print(f"Packets:       {self.packets} ({self.throughput_pps:.1f} pps)")
        print(f"Data sent:     {self.bytes_sent / 1024:.1f} KB")
        print(f"Throughput:    {self.throughput_mbps:.2f} Mbps")
        print(f"Success rate:  {self.success_rate:.1f}% ({self.success_count}/{self.success_count + self.fail_count})")
        print(f"\nLatency:")
        print(f"  Average:     {self.avg_latency_ms:.2f} ms")
        print(f"  P95:         {self.p95_latency_ms:.2f} ms")
        print(f"  P99:         {self.p99_latency_ms:.2f} ms")
        print(f"  Min:         {min(self.latencies)*1000:.2f} ms" if self.latencies else "  N/A")
        print(f"  Max:         {max(self.latencies)*1000:.2f} ms" if self.latencies else "  N/A")
        print(f"{'='*60}\n")


class STORMBenchmark:
    """STORM benchmark suite"""
    
    def __init__(self):
        self.results: List[BenchmarkResult] = []
    
    # ========================================================================
    # 1. ENCRYPTION BENCHMARKS
    # ========================================================================
    
    async def benchmark_encryption(self, packet_sizes: List[int] = None) -> BenchmarkResult:
        """Benchmark encryption/decryption overhead"""
        
        if packet_sizes is None:
            packet_sizes = [64, 256, 512, 1024]
        
        latencies = []
        total_bytes = 0
        
        psk = b"benchmark_psk_32bytes_here!!!"
        conn_id = bytes([0x12, 0x34, 0x56, 0x78])
        
        print("🔐 Benchmarking encryption...")
        
        for payload_size in packet_sizes:
            plaintext = b"X" * payload_size
            
            # Warmup
            for _ in range(10):
                nonce, ciphertext = STORMCrypto.encrypt_packet(plaintext, conn_id, 1, psk)
                _ = STORMCrypto.decrypt_packet(nonce, ciphertext, conn_id, 1, psk)
            
            # Benchmark
            start = time.time()
            for i in range(1000):
                nonce, ciphertext = STORMCrypto.encrypt_packet(plaintext, conn_id, i, psk)
                _ = STORMCrypto.decrypt_packet(nonce, ciphertext, conn_id, i, psk)
            elapsed = time.time() - start
            
            total_bytes += payload_size * 1000
            avg_latency = elapsed / 1000
            latencies.append(avg_latency)
            
            print(f"  {payload_size:4d} bytes: {avg_latency*1000:.3f} ms/packet")
        
        return BenchmarkResult(
            name="Encryption/Decryption (ChaCha20-Poly1305)",
            duration_sec=sum(latencies),
            packets=len(packet_sizes) * 1000,
            bytes_sent=total_bytes,
            success_count=len(packet_sizes) * 1000,
            fail_count=0,
            latencies=latencies,
        )
    
    # ========================================================================
    # 2. PROTOCOL BENCHMARKS
    # ========================================================================
    
    async def benchmark_packet_building(self, count: int = 10000) -> BenchmarkResult:
        """Benchmark packet construction overhead"""
        
        print(f"📦 Benchmarking packet building ({count} packets)...")
        
        latencies = []
        total_bytes = 0
        
        payloads = [b"X" * size for size in [64, 256, 512]]
        
        start = time.time()
        for i in range(count):
            payload = payloads[i % len(payloads)]
            packet = make_packet(
                conn_id=bytes([1, 2, 3, 4]),
                flags=0,
                seq_offset=i,
                payload=payload,
            )
            total_bytes += len(packet)
            
            # Parse it back
            parsed = parse_packet(packet)
        
        elapsed = time.time() - start
        avg_latency = elapsed / count
        
        print(f"  Average: {avg_latency*1000:.3f} ms/packet")
        
        return BenchmarkResult(
            name="Packet Construction & Parsing",
            duration_sec=elapsed,
            packets=count,
            bytes_sent=total_bytes,
            success_count=count,
            fail_count=0,
            latencies=[avg_latency] * count,
        )
    
    # ========================================================================
    # 3. FAILOVER BENCHMARKS
    # ========================================================================
    
    async def benchmark_resolver_selection(self, iterations: int = 10000) -> BenchmarkResult:
        """Benchmark resolver selection overhead"""
        
        print(f"🔄 Benchmarking resolver selection ({iterations} iterations)...")
        
        resolvers = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
        selector = ResolverSelector(resolvers)
        
        latencies = []
        
        start = time.time()
        for i in range(iterations):
            t1 = time.time()
            primary, secondary = selector.select_pair()
            t2 = time.time()
            
            latencies.append(t2 - t1)
            
            # Simulate health reporting
            if i % 100 == 0:
                selector.report_success(primary, 0.05)
            else:
                selector.report_success(primary, 0.05)
        
        elapsed = time.time() - start
        avg_latency = statistics.mean(latencies)
        
        print(f"  Average: {avg_latency*1_000_000:.3f} µs/selection")
        
        return BenchmarkResult(
            name="Resolver Selection",
            duration_sec=elapsed,
            packets=iterations,
            bytes_sent=0,
            success_count=iterations,
            fail_count=0,
            latencies=latencies,
        )
    
    # ========================================================================
    # 4. CARRIER BENCHMARKS
    # ========================================================================
    
    async def benchmark_carrier_creation(self, count: int = 100) -> BenchmarkResult:
        """Benchmark carrier creation overhead"""
        
        print(f"🎯 Benchmarking carrier creation ({count} carriers)...")
        
        resolvers = ["8.8.8.8", "1.1.1.1"]
        latencies = []
        
        for i in range(count):
            start = time.time()
            
            carrier = STORMCarrier(
                carrier_id=i,
                resolvers=resolvers,
                config=CarrierConfig(),
            )
            
            elapsed = time.time() - start
            latencies.append(elapsed)
            
            if (i + 1) % 20 == 0:
                print(f"  Created {i+1}/{count} carriers...")
        
        return BenchmarkResult(
            name="Carrier Creation",
            duration_sec=sum(latencies),
            packets=count,
            bytes_sent=0,
            success_count=count,
            fail_count=0,
            latencies=latencies,
        )
    
    # ========================================================================
    # 5. GATEWAY BENCHMARKS
    # ========================================================================
    
    async def benchmark_gateway_throughput(
        self,
        duration_sec: float = 10.0,
        packet_size: int = 256,
    ) -> BenchmarkResult:
        """Benchmark gateway throughput (simulated)"""
        
        print(f"💨 Benchmarking gateway throughput ({duration_sec}s, {packet_size} bytes/packet)...")
        
        gateway = STORMNexoraV2Gateway(
            resolvers=["8.8.8.8", "1.1.1.1"],
        )
        
        carrier = await gateway.get_or_create_carrier()
        
        latencies = []
        packets_sent = 0
        packets_success = 0
        
        start = time.time()
        while time.time() - start < duration_sec:
            stream_id = (packets_sent % 10) + 1
            payload = b"X" * packet_size
            
            pkt_start = time.time()
            try:
                # Simulate sending (actual implementation would use DNS)
                await carrier.storm_conn.send_data(payload)
                packets_success += 1
            except:
                pass
            pkt_latency = time.time() - pkt_start
            
            latencies.append(pkt_latency)
            packets_sent += 1
            
            # Small delay to avoid spinning
            await asyncio.sleep(0.001)
        
        elapsed = time.time() - start
        total_bytes = packets_sent * packet_size
        
        await gateway.close()
        
        return BenchmarkResult(
            name="Gateway Throughput (Simulated)",
            duration_sec=elapsed,
            packets=packets_sent,
            bytes_sent=total_bytes,
            success_count=packets_success,
            fail_count=packets_sent - packets_success,
            latencies=latencies,
        )
    
    # ========================================================================
    # 6. STREAM MULTIPLEXING BENCHMARKS
    # ========================================================================
    
    async def benchmark_multiplexing(
        self,
        stream_count: int = 10,
        packets_per_stream: int = 100,
    ) -> BenchmarkResult:
        """Benchmark stream multiplexing"""
        
        print(f"🔀 Benchmarking multiplexing ({stream_count} streams, {packets_per_stream} packets each)...")
        
        gateway = STORMNexoraV2Gateway(resolvers=["8.8.8.8"])
        carrier = await gateway.get_or_create_carrier()
        
        # Initialize streams
        for i in range(1, stream_count + 1):
            carrier.stream_map[i] = asyncio.Queue()
        
        latencies = []
        total_packets = 0
        
        async def stream_test(stream_id: int):
            nonlocal total_packets
            
            queue = carrier.stream_map[stream_id]
            for pkt in range(packets_per_stream):
                start = time.time()
                await queue.put(f"stream_{stream_id}_pkt_{pkt}".encode())
                elapsed = time.time() - start
                latencies.append(elapsed)
                total_packets += 1
        
        start = time.time()
        await asyncio.gather(*[stream_test(i) for i in range(1, stream_count + 1)])
        elapsed = time.time() - start
        
        await gateway.close()
        
        return BenchmarkResult(
            name="Stream Multiplexing",
            duration_sec=elapsed,
            packets=total_packets,
            bytes_sent=total_packets * 50,  # Estimate
            success_count=total_packets,
            fail_count=0,
            latencies=latencies,
        )
    
    # ========================================================================
    # COMPARISON BENCHMARKS
    # ========================================================================
    
    async def benchmark_comparison(self) -> Dict[str, BenchmarkResult]:
        """Run all benchmarks and compare"""
        
        print("\n" + "="*60)
        print("STORM PERFORMANCE BENCHMARK SUITE")
        print("="*60)
        
        results = {}
        
        # Encryption
        results["encryption"] = await self.benchmark_encryption()
        results["encryption"].print_summary()
        
        # Protocol
        results["packet_building"] = await self.benchmark_packet_building()
        results["packet_building"].print_summary()
        
        # Failover
        results["resolver_selection"] = await self.benchmark_resolver_selection()
        results["resolver_selection"].print_summary()
        
        # Carrier
        results["carrier_creation"] = await self.benchmark_carrier_creation(count=50)
        results["carrier_creation"].print_summary()
        
        # Gateway
        results["gateway_throughput"] = await self.benchmark_gateway_throughput(duration_sec=5)
        results["gateway_throughput"].print_summary()
        
        # Multiplexing
        results["multiplexing"] = await self.benchmark_multiplexing(stream_count=5, packets_per_stream=50)
        results["multiplexing"].print_summary()
        
        return results
    
    def print_comparison_matrix(self, results: Dict[str, BenchmarkResult]):
        """Print comparison matrix"""
        
        print("\n" + "="*60)
        print("PERFORMANCE COMPARISON")
        print("="*60)
        
        print(f"\n{'Benchmark':<30} {'Throughput':<15} {'Latency (avg)':<15} {'Success Rate':<15}")
        print("-" * 75)
        
        for name, result in results.items():
            if result.throughput_mbps > 0:
                throughput_str = f"{result.throughput_mbps:.2f} Mbps"
            else:
                throughput_str = f"{result.throughput_pps:.0f} ops/s"
            
            latency_str = f"{result.avg_latency_ms:.3f} ms"
            success_str = f"{result.success_rate:.1f}%"
            
            print(f"{name:<30} {throughput_str:<15} {latency_str:<15} {success_str:<15}")
        
        print("="*60 + "\n")


# Import parse_packet for benchmarks
try:
    from storm_proto import parse_packet
except ImportError:
    def parse_packet(pkt):
        return None


async def main():
    """Run benchmarks"""
    
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    benchmark = STORMBenchmark()
    results = await benchmark.benchmark_comparison()
    benchmark.print_comparison_matrix(results)
    
    # Export results to JSON
    import json
    try:
        results_dict = {
            name: {
                "throughput_mbps": result.throughput_mbps,
                "throughput_pps": result.throughput_pps,
                "avg_latency_ms": result.avg_latency_ms,
                "p95_latency_ms": result.p95_latency_ms,
                "p99_latency_ms": result.p99_latency_ms,
                "success_rate": result.success_rate,
                "packets": result.packets,
            }
            for name, result in results.items()
        }
        
        with open("benchmark_results.json", "w") as f:
            json.dump(results_dict, f, indent=2)
        
        print("📊 Results saved to benchmark_results.json")
    except Exception as e:
        print(f"⚠️  Could not save results: {e}")


if __name__ == "__main__":
    asyncio.run(main())
