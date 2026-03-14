#!/usr/bin/env python3
"""
Rank STORM resolvers by real end-to-end SOCKS success rate.

This script automates the manual workflow:
1) Pin one resolver in `resolvers_active.txt`
2) Restart storm-client
3) Run multiple SOCKS probes through STORM
4) Rank resolvers by success-rate/p95
5) Write top-N resolvers back to active file
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
import shlex
import subprocess
import time
from typing import Any

from storm_health_check import ProbeResult, compute_stats, run_socks5_probe


def parse_resolver_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in text.replace("\n", " ").split(" "):
        ip = token.strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def load_tokens_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_resolver_tokens(f.read())
    except FileNotFoundError:
        return []


def load_scanner_candidates(path: str, max_items: int) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    picks: list[str] = []
    rows = data.get("resolvers", [])
    if isinstance(rows, list):
        ranked = sorted(
            [r for r in rows if isinstance(r, dict)],
            key=lambda r: (
                -(float(r.get("pass_rate", 0.0) or 0.0)),
                float(r.get("latency_ms", 1e9) or 1e9),
            ),
        )
        picks.extend(str(r.get("resolver", "")).strip() for r in ranked)

    pools = data.get("pools", {})
    if isinstance(pools, dict):
        for key in ("active", "standby"):
            vals = pools.get(key, [])
            if isinstance(vals, list):
                picks.extend(str(x).strip() for x in vals)

    picks.extend(str(x).strip() for x in (data.get("resolver_list", []) or []))
    deduped = parse_resolver_tokens(" ".join(picks))
    return deduped[: max(1, int(max_items))]


def atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)


def run_cmd(cmdline: str) -> tuple[bool, str]:
    if not cmdline.strip():
        return False, "empty cmd"
    try:
        proc = subprocess.run(
            shlex.split(cmdline),
            capture_output=True,
            text=True,
            check=False,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or "").strip()
        if not detail:
            detail = f"exit={proc.returncode}"
        return ok, detail
    except Exception as exc:
        return False, str(exc)


@dataclass
class ResolverRankRow:
    resolver: str
    success_rate: float
    latency_p95_ms: float
    latency_avg_ms: float
    success: int
    total: int
    errors: list[str]


async def probe_resolver(args: argparse.Namespace, resolver: str) -> ResolverRankRow:
    atomic_write_text(args.active_file, f"{resolver}\n")
    restart_ok, restart_detail = run_cmd(args.restart_cmd)
    if not restart_ok:
        return ResolverRankRow(
            resolver=resolver,
            success_rate=0.0,
            latency_p95_ms=0.0,
            latency_avg_ms=0.0,
            success=0,
            total=args.checks_per_resolver,
            errors=[f"restart-failed: {restart_detail}"],
        )

    if args.settle_sec > 0:
        await asyncio.sleep(args.settle_sec)

    results: list[ProbeResult] = []
    for idx in range(args.checks_per_resolver):
        result = await run_socks5_probe(
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            target_host=args.target_host,
            target_port=args.target_port,
            timeout=args.timeout,
        )
        results.append(result)
        if idx < args.checks_per_resolver - 1 and args.interval > 0:
            await asyncio.sleep(args.interval)

    stats = compute_stats(results)
    return ResolverRankRow(
        resolver=resolver,
        success_rate=float(stats.get("success_rate", 0.0) or 0.0),
        latency_p95_ms=float(stats.get("latency_p95_ms", 0.0) or 0.0),
        latency_avg_ms=float(stats.get("latency_avg_ms", 0.0) or 0.0),
        success=int(stats.get("success", 0) or 0),
        total=int(stats.get("total", 0) or 0),
        errors=list(stats.get("errors", []) or []),
    )


def rank_rows(rows: list[ResolverRankRow]) -> list[ResolverRankRow]:
    return sorted(
        rows,
        key=lambda r: (
            -r.success_rate,
            r.latency_p95_ms if r.success_rate > 0 else 1e9,
            r.latency_avg_ms if r.success_rate > 0 else 1e9,
            r.resolver,
        ),
    )


def select_top(rows: list[ResolverRankRow], take: int) -> list[str]:
    ranked = rank_rows(rows)
    winners = [r.resolver for r in ranked if r.success_rate > 0][: max(1, int(take))]
    if winners:
        return winners
    return [r.resolver for r in ranked[: max(1, int(take))]]


def collect_candidates(args: argparse.Namespace) -> list[str]:
    merged: list[str] = []
    merged.extend(load_tokens_file(args.candidates_file))
    merged.extend(load_tokens_file(args.fallback_file))
    merged.extend(load_scanner_candidates(args.scanner_json, args.scanner_max_items))
    return parse_resolver_tokens(" ".join(merged))[: max(1, int(args.max_candidates))]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rank STORM resolvers by real SOCKS e2e probes")
    p.add_argument("--candidates-file", default="state/resolvers_active.txt")
    p.add_argument("--fallback-file", default="state/resolvers_healthy.txt")
    p.add_argument("--scanner-json", default="state/resolvers_scan.json")
    p.add_argument("--scanner-max-items", type=int, default=64)
    p.add_argument("--max-candidates", type=int, default=16)
    p.add_argument("--active-file", default="state/resolvers_active.txt")
    p.add_argument("--take", type=int, default=4)

    p.add_argument("--proxy-host", default="127.0.0.1")
    p.add_argument("--proxy-port", type=int, default=1443)
    p.add_argument("--target-host", default="65.109.221.2")
    p.add_argument("--target-port", type=int, default=8443)
    p.add_argument("--checks-per-resolver", type=int, default=8)
    p.add_argument("--interval", type=float, default=0.4)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--settle-sec", type=float, default=2.0)
    p.add_argument("--restart-cmd", default="systemctl restart storm-client")
    p.add_argument("--json-out", default="state/resolver_e2e_rank_report.json")
    p.add_argument("--log", action="store_true", help="print per-resolver progress")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    candidates = collect_candidates(args)
    if not candidates:
        print("ERROR: no candidate resolvers found")
        return 2

    rows: list[ResolverRankRow] = []
    for idx, resolver in enumerate(candidates, start=1):
        row = await probe_resolver(args, resolver)
        rows.append(row)
        if args.log:
            print(
                f"[{idx:02d}/{len(candidates):02d}] {resolver} "
                f"sr={row.success_rate:.3f} p95={row.latency_p95_ms:.1f}"
            )

    ranked = rank_rows(rows)
    selected = select_top(rows, args.take)
    atomic_write_text(args.active_file, " ".join(selected).strip() + "\n")
    run_cmd(args.restart_cmd)

    payload: dict[str, Any] = {
        "timestamp": time.time(),
        "candidates": candidates,
        "selected": selected,
        "rows": [asdict(r) for r in ranked],
    }
    atomic_write_text(args.json_out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    print(" ".join(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
