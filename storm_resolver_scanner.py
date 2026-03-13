#!/usr/bin/env python3
"""
STORM resolver scanner sidecar.

Continuously probes resolver candidates with protocol-aware checks and publishes
stable active/standby pools with rollback protection.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import ipaddress
import json
import logging
import os
import secrets
import statistics
import time
from typing import Any

from storm_dns import DNSTransport
from storm_proto import PacketFlags, make_packet, parse_packet
from storm_resolver_picker import choose_probe_candidates, load_resolvers_file

log = logging.getLogger("storm-resolver-scanner")


@dataclass
class ScanRow:
    resolver: str
    ok: bool
    successes: int
    probes: int
    pass_rate: float
    latency_ms: float
    score: float
    error: str
    pool: str = ""


def _is_public_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
        return addr.version == 4 and addr.is_global
    except ValueError:
        return False


def _dedupe_resolvers(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        ip = str(raw).strip()
        if not ip or ip in seen:
            continue
        if not _is_public_ipv4(ip):
            continue
        seen.add(ip)
        out.append(ip)
    return out


def _row_rank_key(row: ScanRow) -> tuple[float, float, str]:
    return (-row.pass_rate, row.latency_ms, row.resolver)


def _rows_to_dicts(rows: list[ScanRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]


def _dicts_to_rows(rows: list[dict[str, Any]]) -> list[ScanRow]:
    out: list[ScanRow] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        resolver = str(item.get("resolver", "")).strip()
        if not _is_public_ipv4(resolver):
            continue
        out.append(
            ScanRow(
                resolver=resolver,
                ok=bool(item.get("ok", True)),
                successes=int(item.get("successes", 0) or 0),
                probes=max(1, int(item.get("probes", 1) or 1)),
                pass_rate=float(item.get("pass_rate", 0.0) or 0.0),
                latency_ms=float(item.get("latency_ms", 0.0) or 0.0),
                score=float(item.get("score", 0.0) or 0.0),
                error=str(item.get("error", "") or ""),
                pool=str(item.get("pool", "") or ""),
            )
        )
    return out


def _classify_pools(
    publish_rows: list[ScanRow],
    failed_rows: list[ScanRow],
    active_pool_size: int,
    standby_pool_size: int,
) -> tuple[list[str], list[str], list[str]]:
    ranked = sorted(publish_rows, key=_row_rank_key)
    active_n = max(1, int(active_pool_size))
    standby_n = max(0, int(standby_pool_size))

    active = [r.resolver for r in ranked[:active_n]]
    standby = [r.resolver for r in ranked[active_n : active_n + standby_n]]
    quarantine = [r.resolver for r in failed_rows]
    return active, standby, quarantine


def _build_resolver_list(
    active: list[str],
    standby: list[str],
    publish_rows: list[ScanRow],
    previous_resolver_list: list[str],
    source_candidates: list[str],
    max_items: int = 512,
) -> list[str]:
    merged: list[str] = []
    merged.extend(active)
    merged.extend(standby)
    merged.extend(r.resolver for r in sorted(publish_rows, key=_row_rank_key))
    merged.extend(previous_resolver_list)
    merged.extend(source_candidates)
    deduped = _dedupe_resolvers(merged)
    return deduped[: max(1, int(max_items))]


def _mark_row_pools(
    rows: list[ScanRow],
    active: set[str],
    standby: set[str],
) -> list[ScanRow]:
    out: list[ScanRow] = []
    for row in rows:
        pool = "quarantine"
        if row.resolver in active:
            pool = "active"
        elif row.resolver in standby:
            pool = "standby"
        out.append(
            ScanRow(
                resolver=row.resolver,
                ok=row.ok,
                successes=row.successes,
                probes=row.probes,
                pass_rate=row.pass_rate,
                latency_ms=row.latency_ms,
                score=row.score,
                error=row.error,
                pool=pool,
            )
        )
    return out


def _load_previous_report(path: str) -> tuple[list[ScanRow], list[str], float]:
    if not path or not os.path.isfile(path):
        return [], [], 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = _dicts_to_rows(data.get("resolvers", []))
        resolver_list = _dedupe_resolvers(data.get("resolver_list", []))
        ts = float(data.get("timestamp_ts", 0.0) or 0.0)
        return rows, resolver_list, ts
    except Exception:
        return [], [], 0.0


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


async def _probe_once(
    transport: DNSTransport,
    resolver: str,
    timeout: float,
) -> tuple[bool, float, str]:
    conn_id = secrets.token_bytes(4)
    packet = make_packet(
        conn_id=conn_id,
        flags=PacketFlags.KEEPALIVE,
        seq_offset=0,
        payload=b"",
    )
    try:
        resp, latency = await transport.send_query(
            packet=packet,
            resolver_ip=resolver,
            session_id="scan",
            timeout=timeout,
        )
        if resp is None:
            return False, latency, "no-response"
        header, _ = parse_packet(resp)
        if header.conn_id != conn_id:
            return False, latency, "conn-id-mismatch"
        return True, latency, ""
    except Exception as exc:
        return False, timeout * 1000.0, str(exc)


async def _probe_resolver(
    transport: DNSTransport,
    resolver: str,
    timeout: float,
    rounds: int,
    min_success: int,
) -> ScanRow:
    probes = max(1, int(rounds))
    need = max(1, min(int(min_success), probes))
    successes = 0
    latency_samples: list[float] = []
    error = ""

    for idx in range(probes):
        ok, latency_ms, err = await _probe_once(transport, resolver, timeout)
        if ok:
            successes += 1
            latency_samples.append(latency_ms)
        elif err:
            error = err
        if idx + 1 < probes:
            await asyncio.sleep(0.03)

    pass_rate = successes / probes
    latency_ms = (
        float(statistics.median(latency_samples))
        if latency_samples
        else timeout * 1000.0
    )
    ok = successes >= need
    score = (pass_rate * 1000.0) - latency_ms

    return ScanRow(
        resolver=resolver,
        ok=ok,
        successes=successes,
        probes=probes,
        pass_rate=round(pass_rate, 4),
        latency_ms=round(latency_ms, 2),
        score=round(score, 2),
        error=error if not ok else "",
    )


async def run_scan_once(args: argparse.Namespace) -> dict[str, Any]:
    source_candidates = load_resolvers_file(args.resolvers_file)
    if not source_candidates:
        raise RuntimeError("no valid resolvers found in resolver file")

    candidates = choose_probe_candidates(
        source_candidates,
        max_probe=args.max_probe,
        sample_mode=args.sample_mode,
    )
    candidates = _dedupe_resolvers(candidates)
    if not candidates:
        raise RuntimeError("no valid public resolver candidates selected")

    transport = DNSTransport(zone=args.zone, qtype=args.qtype)
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _run(ip: str) -> ScanRow:
        async with sem:
            return await _probe_resolver(
                transport=transport,
                resolver=ip,
                timeout=args.timeout,
                rounds=args.rounds,
                min_success=args.min_success,
            )

    tasks = [asyncio.create_task(_run(ip)) for ip in candidates]
    rows = await asyncio.gather(*tasks)
    rows.sort(key=_row_rank_key)

    publish_rows = [
        row
        for row in rows
        if row.ok
        and row.pass_rate >= args.publish_min_pass_rate
        and (args.publish_max_latency_ms <= 0 or row.latency_ms <= args.publish_max_latency_ms)
        and row.score >= args.publish_min_score
    ]
    failed_rows = [row for row in rows if row.resolver not in {r.resolver for r in publish_rows}]

    prev_rows, prev_resolver_list, prev_ts = _load_previous_report(args.output)
    now_ts = time.time()
    min_publish = max(2, min(4, max(1, int(args.active_pool_size))))
    rollback_used = False
    if len(publish_rows) < min_publish and prev_rows and (now_ts - prev_ts) <= args.rollback_ttl:
        rollback_used = True
        publish_rows = sorted(prev_rows, key=_row_rank_key)
        failed_rows = [row for row in rows if row.resolver not in {r.resolver for r in publish_rows}]

    active, standby, quarantine = _classify_pools(
        publish_rows=publish_rows,
        failed_rows=failed_rows,
        active_pool_size=args.active_pool_size,
        standby_pool_size=args.standby_pool_size,
    )
    marked_rows = _mark_row_pools(publish_rows + failed_rows, set(active), set(standby))

    resolver_list = _build_resolver_list(
        active=active,
        standby=standby,
        publish_rows=publish_rows,
        previous_resolver_list=prev_resolver_list,
        source_candidates=source_candidates,
    )

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "timestamp_ts": now_ts,
        "zone": args.zone,
        "total_scanned": len(rows),
        "total_working": len(publish_rows),
        "rollback_used": rollback_used,
        "metrics": {
            "publish_min_pass_rate": args.publish_min_pass_rate,
            "publish_max_latency_ms": args.publish_max_latency_ms,
            "publish_min_score": args.publish_min_score,
            "rounds": args.rounds,
            "min_success": args.min_success,
            "global_pass_rate_avg": round(
                statistics.mean([row.pass_rate for row in rows]) if rows else 0.0,
                4,
            ),
        },
        "pools": {
            "active": active,
            "standby": standby,
            "quarantine": quarantine,
        },
        "resolvers": _rows_to_dicts(marked_rows),
        "resolver_list": resolver_list,
    }
    return report


async def run_loop(args: argparse.Namespace) -> int:
    while True:
        started = time.time()
        try:
            report = await run_scan_once(args)
            _atomic_write_json(args.output, report)
            log.info(
                "publish scanned=%d working=%d active=%d standby=%d rollback=%s",
                report["total_scanned"],
                report["total_working"],
                len(report["pools"]["active"]),
                len(report["pools"]["standby"]),
                report["rollback_used"],
            )
        except Exception:
            log.exception("scanner cycle failed")

        if args.loop <= 0:
            return 0
        elapsed = time.time() - started
        await asyncio.sleep(max(1.0, args.loop - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STORM resolver scanner sidecar")
    parser.add_argument("--resolvers-file", default="data/resolvers.txt")
    parser.add_argument("--output", default="state/resolvers_scan.json")
    parser.add_argument("--zone", default="t1.phonexpress.ir")
    parser.add_argument("--qtype", default="TXT")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--max-probe", type=int, default=400)
    parser.add_argument("--sample-mode", choices=["head", "random"], default="random")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--min-success", type=int, default=1)
    parser.add_argument("--active-pool-size", type=int, default=12)
    parser.add_argument("--standby-pool-size", type=int, default=64)
    parser.add_argument("--publish-min-pass-rate", type=float, default=0.55)
    parser.add_argument("--publish-max-latency-ms", type=float, default=900.0)
    parser.add_argument("--publish-min-score", type=float, default=0.0)
    parser.add_argument("--rollback-ttl", type=float, default=1800.0)
    parser.add_argument("--loop", type=float, default=120.0, help="seconds between cycles; <=0 means run once")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    return await run_loop(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
