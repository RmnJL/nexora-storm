#!/usr/bin/env python3
"""
STORM Tunnel Health Check

Active probe for the stable STORM path:
  app/xray (SOCKS client) -> storm_client (inside) -> DNS tunnel -> storm_server (outside) -> SOCKS upstream

The probe performs SOCKS5 greeting + CONNECT and reports:
  - success rate (loss/failure proxy)
  - latency distribution
  - reconnect behavior across repeated sessions
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float
    error: str | None
    timestamp: float


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = max(0.0, min(1.0, q)) * (len(values) - 1)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    frac = rank - low
    return values[low] * (1 - frac) + values[high] * frac


def _build_connect_request(target_host: str, target_port: int) -> bytes:
    try:
        ip_obj = ipaddress.ip_address(target_host)
        if ip_obj.version == 4:
            atyp = b"\x01"
            addr = ip_obj.packed
        else:
            atyp = b"\x04"
            addr = ip_obj.packed
    except ValueError:
        encoded = target_host.encode("idna")
        if len(encoded) > 255:
            raise ValueError("target host is too long for SOCKS5 domain field")
        atyp = b"\x03"
        addr = bytes([len(encoded)]) + encoded
    
    if not (1 <= target_port <= 65535):
        raise ValueError("target port must be in range 1..65535")
    
    port = target_port.to_bytes(2, "big")
    return b"\x05\x01\x00" + atyp + addr + port


async def _read_socks5_reply(
    reader: asyncio.StreamReader,
    timeout: float,
) -> tuple[int, int]:
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    ver, rep, _, atyp = header
    if ver != 0x05:
        raise RuntimeError(f"bad SOCKS version in reply: {ver}")
    
    if atyp == 0x01:  # IPv4
        await asyncio.wait_for(reader.readexactly(4 + 2), timeout=timeout)
    elif atyp == 0x03:  # domain
        ln = await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
        await asyncio.wait_for(reader.readexactly(ln[0] + 2), timeout=timeout)
    elif atyp == 0x04:  # IPv6
        await asyncio.wait_for(reader.readexactly(16 + 2), timeout=timeout)
    else:
        raise RuntimeError(f"unsupported SOCKS atyp in reply: {atyp}")
    
    return ver, rep


async def run_socks5_probe(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> ProbeResult:
    start = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=timeout,
        )
        
        # Greeting: VER=5, NMETHODS=1, METHODS=[no-auth]
        writer.write(b"\x05\x01\x00")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        greeting_reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if greeting_reply != b"\x05\x00":
            raise RuntimeError(f"SOCKS greeting failed: {greeting_reply.hex()}")
        
        # CONNECT request
        req = _build_connect_request(target_host, target_port)
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        
        _, rep = await _read_socks5_reply(reader, timeout=timeout)
        if rep != 0x00:
            raise RuntimeError(f"SOCKS CONNECT failed with REP=0x{rep:02x}")
        
        elapsed = (time.perf_counter() - start) * 1000.0
        return ProbeResult(ok=True, latency_ms=elapsed, error=None, timestamp=time.time())
    
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            ok=False,
            latency_ms=elapsed,
            error=str(exc),
            timestamp=time.time(),
        )
    
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def compute_stats(results: list[ProbeResult]) -> dict[str, Any]:
    total = len(results)
    ok_results = [r for r in results if r.ok]
    failures = [r for r in results if not r.ok]
    latencies = sorted(r.latency_ms for r in ok_results)
    success = len(ok_results)
    success_rate = (success / total) if total else 0.0
    
    stats: dict[str, Any] = {
        "total": total,
        "success": success,
        "failures": len(failures),
        "success_rate": success_rate,
    }
    
    if latencies:
        stats.update(
            {
                "latency_min_ms": min(latencies),
                "latency_avg_ms": statistics.mean(latencies),
                "latency_max_ms": max(latencies),
                "latency_p50_ms": _percentile(latencies, 0.50),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "latency_p99_ms": _percentile(latencies, 0.99),
            }
        )
    else:
        stats.update(
            {
                "latency_min_ms": 0.0,
                "latency_avg_ms": 0.0,
                "latency_max_ms": 0.0,
                "latency_p50_ms": 0.0,
                "latency_p95_ms": 0.0,
                "latency_p99_ms": 0.0,
            }
        )
    
    if failures:
        stats["errors"] = [r.error for r in failures[:10]]
    else:
        stats["errors"] = []
    
    return stats


def print_report(stats: dict[str, Any]) -> None:
    print("=" * 60)
    print("STORM Health Check Report")
    print("=" * 60)
    print(f"Checks:         {stats['total']}")
    print(f"Success:        {stats['success']}")
    print(f"Failures:       {stats['failures']}")
    print(f"Success Rate:   {stats['success_rate'] * 100:.1f}%")
    print(f"Latency (ms):   min={stats['latency_min_ms']:.2f} avg={stats['latency_avg_ms']:.2f} max={stats['latency_max_ms']:.2f}")
    print(f"Latency p50/p95/p99: {stats['latency_p50_ms']:.2f}/{stats['latency_p95_ms']:.2f}/{stats['latency_p99_ms']:.2f}")
    if stats["errors"]:
        print("Recent Errors:")
        for err in stats["errors"]:
            print(f"  - {err}")
    print("=" * 60)


async def run_batch(args: argparse.Namespace) -> tuple[list[ProbeResult], dict[str, Any]]:
    results: list[ProbeResult] = []
    for i in range(args.checks):
        result = await run_socks5_probe(
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            target_host=args.target_host,
            target_port=args.target_port,
            timeout=args.timeout,
        )
        results.append(result)
        status = "OK" if result.ok else "FAIL"
        print(f"[{i+1:03d}/{args.checks:03d}] {status:4} {result.latency_ms:8.2f} ms")
        if i < args.checks - 1:
            await asyncio.sleep(args.interval)
    return results, compute_stats(results)


def _save_json(args: argparse.Namespace, results: list[ProbeResult], stats: dict[str, Any]) -> None:
    if not args.json_out:
        return
    payload = {
        "proxy": f"{args.proxy_host}:{args.proxy_port}",
        "target": f"{args.target_host}:{args.target_port}",
        "checks": [asdict(r) for r in results],
        "stats": stats,
        "timestamp": time.time(),
    }
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"JSON report saved to: {args.json_out}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STORM SOCKS end-to-end health check")
    parser.add_argument("--proxy-host", default="127.0.0.1", help="Local STORM client host")
    parser.add_argument("--proxy-port", type=int, default=1443, help="Local STORM client port")
    parser.add_argument("--target-host", default="1.1.1.1", help="SOCKS CONNECT target host")
    parser.add_argument("--target-port", type=int, default=53, help="SOCKS CONNECT target port")
    parser.add_argument("--checks", type=int, default=20, help="Number of reconnect probes")
    parser.add_argument("--interval", type=float, default=0.5, help="Delay between probes (seconds)")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-probe timeout (seconds)")
    parser.add_argument("--success-threshold", type=float, default=0.95, help="Minimum acceptable success rate")
    parser.add_argument("--latency-threshold-ms", type=float, default=1200.0, help="Maximum acceptable p95 latency")
    parser.add_argument("--json-out", default="", help="Write JSON output to file")
    parser.add_argument("--continuous", action="store_true", help="Run batches continuously")
    parser.add_argument("--batch-sleep", type=float, default=5.0, help="Sleep between batches in continuous mode")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    exit_code = 0
    
    while True:
        results, stats = await run_batch(args)
        print_report(stats)
        _save_json(args, results, stats)
        
        if stats["success_rate"] < args.success_threshold:
            print(
                f"HEALTH FAIL: success_rate={stats['success_rate']:.3f} "
                f"< threshold={args.success_threshold:.3f}"
            )
            exit_code = 2
        
        if stats["latency_p95_ms"] > args.latency_threshold_ms:
            print(
                f"HEALTH FAIL: p95={stats['latency_p95_ms']:.2f} ms "
                f"> threshold={args.latency_threshold_ms:.2f} ms"
            )
            exit_code = 2
        
        if not args.continuous:
            return exit_code
        await asyncio.sleep(args.batch_sleep)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
