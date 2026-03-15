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
from storm_proto import FrameType, PacketFlags, make_frame, make_packet, parse_frame, parse_packet
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


def _prefix24_key(ip: str) -> str:
    parts = str(ip).split(".")
    if len(parts) != 4:
        return ""
    return ".".join(parts[:3])


def _limit_prefix24(items: list[str], max_per_prefix24: int) -> list[str]:
    cap = int(max_per_prefix24)
    if cap <= 0:
        return list(items)
    out: list[str] = []
    counts: dict[str, int] = {}
    for ip in items:
        prefix = _prefix24_key(ip)
        if not prefix:
            continue
        cur = counts.get(prefix, 0)
        if cur >= cap:
            continue
        counts[prefix] = cur + 1
        out.append(ip)
    if len(out) >= len(items):
        return out
    # Backfill without prefix cap so pool size can still be reached.
    seen = set(out)
    for ip in items:
        if ip in seen:
            continue
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


def _load_cursor(path: str) -> int:
    if not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return max(0, int(raw or "0"))
    except Exception:
        return 0


def _save_cursor(path: str, cursor: int) -> None:
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{max(0, int(cursor))}\n")
    os.replace(tmp, path)


def _select_sticky_candidates(
    previous_rows: list[ScanRow],
    source_candidates: list[str],
    sticky_rows: int,
    sticky_pools: set[str],
) -> list[str]:
    if sticky_rows <= 0 or not previous_rows:
        return []
    source_set = set(source_candidates)
    ranked_prev = sorted(previous_rows, key=_row_rank_key)
    selected: list[str] = []
    for row in ranked_prev:
        if len(selected) >= sticky_rows:
            break
        pool = str(row.pool).strip().lower()
        if sticky_pools and pool and pool not in sticky_pools:
            continue
        if row.resolver not in source_set:
            continue
        if row.resolver in selected:
            continue
        selected.append(row.resolver)
    return selected


def _select_probe_candidates(
    source_candidates: list[str],
    max_probe: int,
    sample_mode: str,
    cursor: int,
) -> tuple[list[str], int]:
    if not source_candidates:
        return [], cursor

    cap = min(max(1, int(max_probe)), len(source_candidates))
    if cap >= len(source_candidates):
        return list(source_candidates), 0

    if sample_mode in {"head", "random"}:
        picks = choose_probe_candidates(
            source_candidates,
            max_probe=cap,
            sample_mode=sample_mode,
        )
        return _dedupe_resolvers(picks), cursor

    if sample_mode == "sequential":
        start = cursor % len(source_candidates)
        rotated = source_candidates[start:] + source_candidates[:start]
        picks = rotated[:cap]
        new_cursor = (start + cap) % len(source_candidates)
        return _dedupe_resolvers(picks), new_cursor

    raise ValueError(f"unsupported sample_mode: {sample_mode}")


def _resolve_phase1_limits(
    max_probe: int,
    prefilter_keep: int,
    quick_max_probe: int,
    quick_keep: int,
) -> tuple[int, int, int, int]:
    deep_cap = max(1, int(max_probe))

    if int(prefilter_keep) > 0:
        prefilter_cap = max(deep_cap, int(prefilter_keep))
    else:
        prefilter_cap = max(1000, deep_cap * 4)

    if int(quick_max_probe) > 0:
        quick_probe_cap = min(prefilter_cap, max(1, int(quick_max_probe)))
    else:
        quick_probe_cap = min(prefilter_cap, max(deep_cap, deep_cap * 3))

    if int(quick_keep) > 0:
        quick_keep_cap = min(prefilter_cap, max(1, int(quick_keep)))
    else:
        quick_keep_cap = deep_cap

    return deep_cap, prefilter_cap, quick_probe_cap, quick_keep_cap


def _build_prefilter_candidates(
    source_candidates: list[str],
    sampled_candidates: list[str],
    sticky_candidates: list[str],
    previous_rows: list[ScanRow],
    previous_resolver_list: list[str],
    prefilter_keep: int,
    prefilter_history_rows: int,
) -> list[str]:
    merged: list[str] = []
    merged.extend(sticky_candidates)
    if prefilter_history_rows > 0:
        ranked_prev = sorted(previous_rows, key=_row_rank_key)
        merged.extend(r.resolver for r in ranked_prev[: max(1, int(prefilter_history_rows))])
    merged.extend(sampled_candidates)
    merged.extend(previous_resolver_list)
    merged.extend(source_candidates)
    return _dedupe_resolvers(merged)[: max(1, int(prefilter_keep))]


async def _run_quick_phase(
    transport: DNSTransport,
    candidates: list[str],
    timeout: float,
    concurrency: int,
    max_probe: int,
) -> list[ScanRow]:
    if not candidates or max_probe <= 0:
        return []
    probe_set = candidates[: max(1, int(max_probe))]
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _run(ip: str) -> ScanRow:
        async with sem:
            ok, latency_ms, err = await _probe_once(
                transport=transport,
                resolver=ip,
                timeout=timeout,
                protocol_roundtrips=1,
                data_probe=False,
            )
            pass_rate = 1.0 if ok else 0.0
            return ScanRow(
                resolver=ip,
                ok=ok,
                successes=1 if ok else 0,
                probes=1,
                pass_rate=pass_rate,
                latency_ms=round(latency_ms, 2),
                score=round((pass_rate * 1000.0) - latency_ms, 2),
                error=err if not ok else "",
            )

    tasks = [asyncio.create_task(_run(ip)) for ip in probe_set]
    rows = await asyncio.gather(*tasks)
    rows.sort(key=lambda row: (0 if row.ok else 1, row.latency_ms, row.resolver))
    return rows


def _select_quick_survivors(rows: list[ScanRow], keep: int) -> list[str]:
    if keep <= 0 or not rows:
        return []
    cap = max(1, int(keep))
    ranked = sorted(rows, key=lambda row: (0 if row.ok else 1, row.latency_ms, row.resolver))
    healthy = [row.resolver for row in ranked if row.ok][:cap]
    if healthy:
        return healthy
    # Fallback: keep the quickest rows if quick phase is fully blocked.
    return [row.resolver for row in ranked[:cap]]


def _classify_pools(
    publish_rows: list[ScanRow],
    failed_rows: list[ScanRow],
    active_pool_size: int,
    standby_pool_size: int,
    max_per_prefix24: int,
) -> tuple[list[str], list[str], list[str]]:
    ranked = sorted(publish_rows, key=_row_rank_key)
    active_n = max(1, int(active_pool_size))
    standby_n = max(0, int(standby_pool_size))

    diverse = _limit_prefix24([r.resolver for r in ranked], max_per_prefix24=max_per_prefix24)
    active = diverse[:active_n]
    standby = diverse[active_n : active_n + standby_n]
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
        rows_all = _dicts_to_rows(data.get("resolvers", []))
        rows = [r for r in rows_all if r.ok and str(r.pool).lower() in {"active", "standby", ""}]
        resolver_list = _dedupe_resolvers(data.get("resolver_list", []))
        ts = float(data.get("timestamp_ts", 0.0) or 0.0)
        rows.sort(key=_row_rank_key)
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
    protocol_roundtrips: int = 1,
    data_probe: bool = False,
) -> tuple[bool, float, str]:
    rounds = max(1, int(protocol_roundtrips))
    latency_samples: list[float] = []
    error = ""

    for _ in range(rounds):
        ok, latency_ms, err = await _probe_protocol_session(
            transport=transport,
            resolver=resolver,
            timeout=timeout,
            extra_keepalive=rounds > 1,
            data_probe=data_probe,
        )
        if not ok:
            return False, latency_ms, err
        latency_samples.append(latency_ms)
        if err:
            error = err

    latency = float(statistics.median(latency_samples)) if latency_samples else timeout * 1000.0
    return True, latency, error


async def _probe_protocol_session(
    transport: DNSTransport,
    resolver: str,
    timeout: float,
    extra_keepalive: bool,
    data_probe: bool,
) -> tuple[bool, float, str]:
    conn_id = secrets.token_bytes(4)
    packet = make_packet(
        conn_id=conn_id,
        flags=PacketFlags.KEEPALIVE,
        seq_offset=0,
        payload=b"",
    )
    try:
        result = await transport.send_query_detailed(
            packet=packet,
            resolver_ip=resolver,
            session_id="scan",
            timeout=timeout,
        )
        if result.response_packet is None:
            err = result.error_class or result.error_detail or "no-response"
            return False, result.latency_ms, err

        header, _ = parse_packet(result.response_packet)
        if header.conn_id != conn_id:
            return False, result.latency_ms, "conn-id-mismatch"

        latencies = [result.latency_ms]
        if data_probe:
            data_frame = make_frame(
                frame_type=FrameType.DATA,
                frame_flags=0,
                seq_group=0,
                payload=secrets.token_bytes(96),
            )
            data_packet = make_packet(
                conn_id=conn_id,
                flags=PacketFlags.DATA,
                seq_offset=1,
                payload=data_frame,
            )
            result_data = await transport.send_query_detailed(
                packet=data_packet,
                resolver_ip=resolver,
                session_id="scan",
                timeout=timeout,
            )
            if result_data.response_packet is None:
                err = result_data.error_class or result_data.error_detail or "no-response"
                return False, result_data.latency_ms, err
            header_data, payload_data = parse_packet(result_data.response_packet)
            if header_data.conn_id != conn_id:
                return False, result_data.latency_ms, "conn-id-mismatch"
            # If server emits DATA, payload must decode as frame.
            if (header_data.flags & PacketFlags.DATA) and payload_data:
                try:
                    parse_frame(payload_data)
                except Exception:
                    return False, result_data.latency_ms, "bad-data-frame"
            latencies.append(result_data.latency_ms)

        if extra_keepalive:
            packet2 = make_packet(
                conn_id=conn_id,
                flags=PacketFlags.KEEPALIVE,
                seq_offset=2 if data_probe else 1,
                payload=b"",
            )
            result2 = await transport.send_query_detailed(
                packet=packet2,
                resolver_ip=resolver,
                session_id="scan",
                timeout=timeout,
            )
            if result2.response_packet is None:
                err = result2.error_class or result2.error_detail or "no-response"
                return False, result2.latency_ms, err
            header2, _ = parse_packet(result2.response_packet)
            if header2.conn_id != conn_id:
                return False, result2.latency_ms, "conn-id-mismatch"
            latencies.append(result2.latency_ms)

        return True, float(statistics.median(latencies)), ""
    except Exception as exc:
        return False, timeout * 1000.0, str(exc)


async def _probe_resolver(
    transport: DNSTransport,
    resolver: str,
    timeout: float,
    rounds: int,
    min_success: int,
    protocol_roundtrips: int,
    data_probe: bool,
) -> ScanRow:
    probes = max(1, int(rounds))
    need = max(1, min(int(min_success), probes))
    successes = 0
    latency_samples: list[float] = []
    error = ""

    for idx in range(probes):
        ok, latency_ms, err = await _probe_once(
            transport=transport,
            resolver=resolver,
            timeout=timeout,
            protocol_roundtrips=protocol_roundtrips,
            data_probe=data_probe,
        )
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

    (
        deep_probe_cap,
        prefilter_cap,
        quick_probe_cap,
        quick_keep_cap,
    ) = _resolve_phase1_limits(
        max_probe=args.max_probe,
        prefilter_keep=args.prefilter_keep,
        quick_max_probe=args.quick_max_probe,
        quick_keep=args.quick_keep,
    )

    prev_rows, prev_resolver_list, prev_ts = _load_previous_report(args.output)

    cursor = int(getattr(args, "_sample_cursor", 0))
    sampled, new_cursor = _select_probe_candidates(
        source_candidates=source_candidates,
        max_probe=deep_probe_cap,
        sample_mode=args.sample_mode,
        cursor=cursor,
    )
    sticky_candidates = _select_sticky_candidates(
        previous_rows=prev_rows,
        source_candidates=source_candidates,
        sticky_rows=args.sticky_rows,
        sticky_pools={x.strip().lower() for x in str(args.sticky_pools).split(",") if x.strip()},
    )
    prefilter_candidates = _build_prefilter_candidates(
        source_candidates=source_candidates,
        sampled_candidates=sampled,
        sticky_candidates=sticky_candidates,
        previous_rows=prev_rows,
        previous_resolver_list=prev_resolver_list,
        prefilter_keep=prefilter_cap,
        prefilter_history_rows=args.prefilter_history_rows,
    )
    setattr(args, "_sample_cursor", new_cursor)
    _save_cursor(args.sample_cursor_file, new_cursor)
    if not prefilter_candidates:
        raise RuntimeError("no valid public resolver candidates selected")

    transport = DNSTransport(zone=args.zone, qtype=args.qtype)
    quick_rows = await _run_quick_phase(
        transport=transport,
        candidates=prefilter_candidates,
        timeout=args.quick_timeout,
        concurrency=args.quick_concurrency,
        max_probe=quick_probe_cap,
    )
    quick_selected = _select_quick_survivors(quick_rows, quick_keep_cap)
    merged_candidates = sticky_candidates + quick_selected + sampled + prefilter_candidates
    candidates = _dedupe_resolvers(merged_candidates)[:deep_probe_cap]
    if not candidates:
        raise RuntimeError("no resolvers available after quick-phase selection")

    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _run(ip: str) -> ScanRow:
        async with sem:
            return await _probe_resolver(
                transport=transport,
                resolver=ip,
                timeout=args.timeout,
                rounds=args.rounds,
                min_success=args.min_success,
                protocol_roundtrips=args.protocol_roundtrips if bool(args.deep_protocol_check) else 1,
                data_probe=bool(args.deep_protocol_check),
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
        max_per_prefix24=args.max_per_prefix24,
    )
    active_set = set(active)
    standby_set = set(standby)
    marked_publish_rows = _mark_row_pools(publish_rows, active_set, standby_set)
    marked_quarantine_rows = _mark_row_pools(failed_rows, set(), set())

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
            "sample_mode": args.sample_mode,
            "sample_cursor": new_cursor,
            "sticky_rows": args.sticky_rows,
            "prefilter_keep": prefilter_cap,
            "prefilter_count": len(prefilter_candidates),
            "prefilter_history_rows": args.prefilter_history_rows,
            "quick_timeout": args.quick_timeout,
            "quick_concurrency": args.quick_concurrency,
            "quick_max_probe": quick_probe_cap,
            "quick_scanned": len(quick_rows),
            "quick_working": sum(1 for row in quick_rows if row.ok),
            "quick_selected": len(quick_selected),
            "quick_keep": quick_keep_cap,
            "deep_probe_cap": deep_probe_cap,
            "global_pass_rate_avg": round(
                statistics.mean([row.pass_rate for row in rows]) if rows else 0.0,
                4,
            ),
            "deep_protocol_check": bool(args.deep_protocol_check),
            "protocol_roundtrips": int(args.protocol_roundtrips),
            "max_per_prefix24": int(args.max_per_prefix24),
        },
        "pools": {
            "active": active,
            "standby": standby,
            "quarantine": quarantine,
        },
        # Keep published resolvers strictly limited to active/standby candidates.
        # Quarantine is exposed separately to avoid feedback loops across rollback cycles.
        "resolvers": _rows_to_dicts(marked_publish_rows),
        "quarantine_resolvers": _rows_to_dicts(marked_quarantine_rows),
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
    parser.add_argument("--sample-cursor-file", default="state/resolvers_scan_cursor.txt")
    parser.add_argument("--zone", default="t1.phonexpress.ir")
    parser.add_argument("--qtype", default="TXT")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--max-probe", type=int, default=400)
    parser.add_argument("--prefilter-keep", type=int, default=0, help="prefilter cap, <=0 means auto")
    parser.add_argument("--prefilter-history-rows", type=int, default=128)
    parser.add_argument("--quick-timeout", type=float, default=1.0)
    parser.add_argument("--quick-concurrency", type=int, default=100)
    parser.add_argument("--quick-max-probe", type=int, default=0, help="quick phase probe cap, <=0 means auto")
    parser.add_argument("--quick-keep", type=int, default=0, help="quick phase survivors cap, <=0 uses --max-probe")
    parser.add_argument("--sample-mode", choices=["head", "random", "sequential"], default="sequential")
    parser.add_argument("--sticky-rows", type=int, default=64)
    parser.add_argument("--sticky-pools", default="active,standby")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--min-success", type=int, default=1)
    parser.add_argument("--deep-protocol-check", type=int, default=1, help="use session-style protocol probe in deep phase")
    parser.add_argument("--protocol-roundtrips", type=int, default=2, help="per-round protocol roundtrips in deep phase")
    parser.add_argument("--active-pool-size", type=int, default=12)
    parser.add_argument("--standby-pool-size", type=int, default=64)
    parser.add_argument("--max-per-prefix24", type=int, default=2, help="max active/standby entries from same /24; <=0 disables")
    parser.add_argument("--publish-min-pass-rate", type=float, default=0.55)
    parser.add_argument("--publish-max-latency-ms", type=float, default=900.0)
    parser.add_argument("--publish-min-score", type=float, default=0.0)
    parser.add_argument("--rollback-ttl", type=float, default=1800.0)
    parser.add_argument("--loop", type=float, default=120.0, help="seconds between cycles; <=0 means run once")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setattr(args, "_sample_cursor", _load_cursor(args.sample_cursor_file))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    return await run_loop(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
