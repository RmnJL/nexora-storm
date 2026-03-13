#!/usr/bin/env python3
"""
Pick healthy DNS resolvers for STORM client from a resolver pool file.

Usage:
  python storm_resolver_picker.py --resolvers-file data/resolvers.txt --zone t1.phonexpress.ir

Outputs a single space-separated resolver list, suitable for:
  python storm_client.py --resolvers <output...>
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import secrets
import random
from dataclasses import asdict, dataclass
import json
from typing import Iterable

from storm_dns import DNSTransport
from storm_proto import PacketFlags, make_packet


@dataclass
class ProbeResult:
    resolver: str
    ok: bool
    latency_ms: float
    error: str | None = None


def _parse_resolver_lines(lines: Iterable[str]) -> list[str]:
    resolvers: list[str] = []
    seen: set[str] = set()

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        token = line.split()[0]
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue

        if token not in seen:
            seen.add(token)
            resolvers.append(token)

    return resolvers


def load_resolvers_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return _parse_resolver_lines(f.readlines())


async def probe_resolver(
    transport: DNSTransport,
    resolver: str,
    timeout: float,
) -> ProbeResult:
    packet = make_packet(
        conn_id=secrets.token_bytes(4),
        flags=PacketFlags.KEEPALIVE,
        seq_offset=0,
        payload=b"",
    )

    try:
        resp, latency = await transport.send_query(
            packet=packet,
            resolver_ip=resolver,
            session_id="probe",
            timeout=timeout,
        )
        if resp is None:
            return ProbeResult(
                resolver=resolver,
                ok=False,
                latency_ms=latency,
                error="no-response",
            )
        return ProbeResult(resolver=resolver, ok=True, latency_ms=latency)
    except Exception as exc:
        return ProbeResult(
            resolver=resolver,
            ok=False,
            latency_ms=timeout * 1000.0,
            error=str(exc),
        )


def rank_resolvers(results: list[ProbeResult], take: int) -> list[str]:
    ranked = sorted(
        results,
        key=lambda r: (
            0 if r.ok else 1,
            r.latency_ms,
        ),
    )
    return [r.resolver for r in ranked[: max(1, take)]]


def choose_probe_candidates(
    resolvers: list[str],
    max_probe: int,
    sample_mode: str,
) -> list[str]:
    if not resolvers:
        return []

    cap = max(1, max_probe)
    if cap >= len(resolvers):
        return list(resolvers)

    if sample_mode == "head":
        return list(resolvers[:cap])

    if sample_mode == "random":
        return random.sample(resolvers, cap)

    raise ValueError(f"unsupported sample_mode: {sample_mode}")


async def probe_pool(
    resolvers: list[str],
    zone: str,
    qtype: str,
    timeout: float,
    max_probe: int,
    concurrency: int,
    sample_mode: str = "head",
) -> list[ProbeResult]:
    probe_set = choose_probe_candidates(resolvers, max_probe, sample_mode)
    transport = DNSTransport(zone=zone, qtype=qtype)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(resolver: str) -> ProbeResult:
        async with sem:
            return await probe_resolver(transport, resolver, timeout)

    tasks = [asyncio.create_task(_run(r)) for r in probe_set]
    return await asyncio.gather(*tasks)


def write_env_file(path: str, selected: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    value = " ".join(selected)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'STORM_RESOLVERS="{value}"\n')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick healthy DNS resolvers for STORM")
    parser.add_argument(
        "--resolvers-file",
        default="data/resolvers.txt",
        help="Path to resolver pool file",
    )
    parser.add_argument("--zone", default="t1.phonexpress.ir", help="Tunnel DNS zone")
    parser.add_argument("--qtype", default="TXT", help="DNS query type")
    parser.add_argument("--timeout", type=float, default=1.5, help="Probe timeout in seconds")
    parser.add_argument("--max-probe", type=int, default=40, help="Probe only first N resolvers")
    parser.add_argument("--concurrency", type=int, default=15, help="Concurrent probes")
    parser.add_argument(
        "--sample-mode",
        choices=["head", "random"],
        default="random",
        help="How to pick resolvers from pool when max-probe < pool size",
    )
    parser.add_argument("--take", type=int, default=4, help="Select top N resolvers")
    parser.add_argument("--min-healthy", type=int, default=2, help="Minimum required healthy resolvers")
    parser.add_argument("--allow-fallback", action="store_true", help="Allow fallback when healthy count is low")
    parser.add_argument("--env-out", default="", help="Write selected resolvers as env file")
    parser.add_argument("--json-out", default="", help="Write probe results as JSON")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    resolvers = load_resolvers_file(args.resolvers_file)
    if not resolvers:
        print("ERROR: no valid resolvers found in file")
        return 2

    results = await probe_pool(
        resolvers=resolvers,
        zone=args.zone,
        qtype=args.qtype,
        timeout=args.timeout,
        max_probe=args.max_probe,
        concurrency=args.concurrency,
        sample_mode=args.sample_mode,
    )

    healthy = [r for r in results if r.ok]
    selected = rank_resolvers(healthy if healthy else results, args.take)
    eligible = len(healthy) >= max(1, args.min_healthy) or args.allow_fallback

    if args.json_out:
        payload = {
            "selected": selected,
            "healthy_count": len(healthy),
            "eligible": eligible,
            "sample_mode": args.sample_mode,
            "probed_count": len(results),
            "results": [asdict(r) for r in results],
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if len(healthy) < max(1, args.min_healthy) and not args.allow_fallback:
        print(
            f"ERROR: healthy resolvers {len(healthy)} < min-healthy {args.min_healthy}. "
            "Use --allow-fallback to continue."
        )
        return 2

    if args.env_out:
        write_env_file(args.env_out, selected)

    print(" ".join(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
