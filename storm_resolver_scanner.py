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


@dataclass
class ResolverRuntimeState:
    resolver: str
    fail_streak: int = 0
    success_streak: int = 0
    last_seen_ok_ts: float = 0.0
    last_seen_fail_ts: float = 0.0
    next_probe_at_ts: float = 0.0
    sr_short: float = 0.0
    sr_long: float = 0.0
    state: str = "candidate"


_RUNTIME_STATES = {"candidate", "probation", "standby", "active", "degraded", "quarantine"}
_DEFAULT_REENTRY_COOLDOWN_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0)


def _normalize_runtime_state(state: str) -> str:
    value = str(state or "").strip().lower()
    if value in _RUNTIME_STATES:
        return value
    return "candidate"


def _runtime_state_counts(runtime: dict[str, ResolverRuntimeState]) -> dict[str, int]:
    counts = {key: 0 for key in sorted(_RUNTIME_STATES)}
    for item in runtime.values():
        counts[_normalize_runtime_state(item.state)] += 1
    return counts


def _parse_cooldown_sequence(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for token in str(raw or "").split(","):
        part = token.strip()
        if not part:
            continue
        try:
            value = float(part)
        except Exception:
            continue
        if value > 0:
            values.append(value)
    if not values:
        return _DEFAULT_REENTRY_COOLDOWN_SECONDS
    return tuple(values)


def _cooldown_for_fail_streak(
    fail_streak: int,
    cooldown_steps: tuple[float, ...],
) -> float:
    if fail_streak <= 0:
        return 0.0
    steps = cooldown_steps or _DEFAULT_REENTRY_COOLDOWN_SECONDS
    idx = min(max(0, int(fail_streak) - 1), len(steps) - 1)
    return max(0.0, float(steps[idx]))


def _is_probe_due_for_runtime(item: ResolverRuntimeState | None, now_ts: float) -> bool:
    if item is None:
        return True
    state = _normalize_runtime_state(item.state)
    if state in {"quarantine", "degraded"} and float(item.next_probe_at_ts) > float(now_ts):
        return False
    return True


def _prioritize_due_candidates(
    candidates: list[str],
    runtime: dict[str, ResolverRuntimeState],
    now_ts: float,
) -> list[str]:
    due: list[str] = []
    delayed: list[str] = []
    for ip in candidates:
        item = runtime.get(ip)
        if _is_probe_due_for_runtime(item, now_ts):
            due.append(ip)
        else:
            delayed.append(ip)
    return due + delayed


def _promote_due_runtime_entries(runtime: dict[str, ResolverRuntimeState], now_ts: float) -> int:
    moved = 0
    for item in runtime.values():
        state = _normalize_runtime_state(item.state)
        if state not in {"quarantine", "degraded"}:
            continue
        if item.next_probe_at_ts <= 0 or float(now_ts) < float(item.next_probe_at_ts):
            continue
        item.state = "probation"
        moved += 1
    return moved


def _ema(prev: float, sample: float, alpha: float) -> float:
    p = max(0.0, min(1.0, float(prev)))
    s = max(0.0, min(1.0, float(sample)))
    a = max(0.0, min(1.0, float(alpha)))
    if p <= 0.0 and s > 0.0:
        return s
    return (a * s) + ((1.0 - a) * p)


def _dict_to_runtime_state(item: dict[str, Any]) -> ResolverRuntimeState | None:
    if not isinstance(item, dict):
        return None
    resolver = str(item.get("resolver", "")).strip()
    if not _is_public_ipv4(resolver):
        return None
    try:
        return ResolverRuntimeState(
            resolver=resolver,
            fail_streak=max(0, int(item.get("fail_streak", 0) or 0)),
            success_streak=max(0, int(item.get("success_streak", 0) or 0)),
            last_seen_ok_ts=max(0.0, float(item.get("last_seen_ok_ts", 0.0) or 0.0)),
            last_seen_fail_ts=max(0.0, float(item.get("last_seen_fail_ts", 0.0) or 0.0)),
            next_probe_at_ts=max(0.0, float(item.get("next_probe_at_ts", 0.0) or 0.0)),
            sr_short=max(0.0, min(1.0, float(item.get("sr_short", 0.0) or 0.0))),
            sr_long=max(0.0, min(1.0, float(item.get("sr_long", 0.0) or 0.0))),
            state=_normalize_runtime_state(str(item.get("state", "candidate") or "candidate")),
        )
    except Exception:
        return None


def _load_runtime_state(path: str) -> dict[str, ResolverRuntimeState]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_rows = data.get("resolvers", [])
        rows: list[dict[str, Any]]
        if isinstance(raw_rows, dict):
            rows = list(raw_rows.values())
        elif isinstance(raw_rows, list):
            rows = [x for x in raw_rows if isinstance(x, dict)]
        else:
            rows = []
        out: dict[str, ResolverRuntimeState] = {}
        for raw in rows:
            parsed = _dict_to_runtime_state(raw)
            if parsed is None:
                continue
            out[parsed.resolver] = parsed
        return out
    except Exception:
        return {}


def _runtime_state_payload(
    runtime: dict[str, ResolverRuntimeState],
    now_ts: float,
) -> dict[str, Any]:
    rows = sorted(runtime.values(), key=lambda item: item.resolver)
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "timestamp_ts": now_ts,
        "total": len(rows),
        "state_counts": _runtime_state_counts(runtime),
        "resolvers": [asdict(item) for item in rows],
    }


def _update_runtime_state(
    previous: dict[str, ResolverRuntimeState],
    rows: list[ScanRow],
    active_set: set[str],
    standby_set: set[str],
    now_ts: float,
    cooldown_steps: tuple[float, ...],
) -> tuple[dict[str, ResolverRuntimeState], dict[str, int]]:
    runtime: dict[str, ResolverRuntimeState] = dict(previous)
    counters = {
        "promotion_count": 0,
        "demotion_count": 0,
        "reentry_count": 0,
    }

    for row in rows:
        prev = runtime.get(row.resolver)
        if prev is None:
            current = ResolverRuntimeState(resolver=row.resolver)
            prev_state = "candidate"
        else:
            current = ResolverRuntimeState(**asdict(prev))
            prev_state = _normalize_runtime_state(prev.state)

        if row.ok:
            current.success_streak += 1
            current.fail_streak = 0
            current.last_seen_ok_ts = now_ts
            current.next_probe_at_ts = now_ts
            current.sr_short = _ema(current.sr_short, row.pass_rate, 0.35)
            current.sr_long = _ema(current.sr_long, row.pass_rate, 0.08)
            if row.resolver in active_set:
                new_state = "active"
            elif row.resolver in standby_set:
                new_state = "standby"
            elif prev_state in {"quarantine", "degraded"}:
                new_state = "probation"
            else:
                new_state = "candidate"
        else:
            current.success_streak = 0
            current.fail_streak = max(0, current.fail_streak) + 1
            current.last_seen_fail_ts = now_ts
            current.next_probe_at_ts = now_ts + _cooldown_for_fail_streak(
                current.fail_streak,
                cooldown_steps,
            )
            current.sr_short = _ema(current.sr_short, 0.0, 0.35)
            current.sr_long = _ema(current.sr_long, 0.0, 0.08)
            new_state = "quarantine" if current.fail_streak >= 3 else "degraded"

        if prev_state != new_state:
            if prev_state in {"active", "standby"} and new_state not in {"active", "standby"}:
                counters["demotion_count"] += 1
            if prev_state not in {"active", "standby"} and new_state in {"active", "standby"}:
                counters["promotion_count"] += 1
            if prev_state == "quarantine" and new_state in {"probation", "candidate", "standby", "active"}:
                counters["reentry_count"] += 1

        current.state = new_state
        runtime[row.resolver] = current

    return runtime, counters


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


def _select_diverse_resolvers(
    ranked_rows: list[ScanRow],
    take: int,
    max_per_prefix24: int,
) -> list[str]:
    target = max(0, int(take))
    if target <= 0:
        return []
    cap = int(max_per_prefix24)
    if cap <= 0:
        return [row.resolver for row in ranked_rows[:target]]
    out: list[str] = []
    counts: dict[str, int] = {}
    for row in ranked_rows:
        prefix = _prefix24_key(row.resolver)
        if not prefix:
            continue
        cur = counts.get(prefix, 0)
        if cur >= cap:
            continue
        counts[prefix] = cur + 1
        out.append(row.resolver)
        if len(out) >= target:
            break
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
    max_active_per_prefix24: int,
    max_standby_per_prefix24: int,
) -> tuple[list[str], list[str], list[str]]:
    ranked = sorted(publish_rows, key=_row_rank_key)
    active_n = max(1, int(active_pool_size))
    standby_n = max(0, int(standby_pool_size))

    active = _select_diverse_resolvers(
        ranked_rows=ranked,
        take=active_n,
        max_per_prefix24=max_active_per_prefix24,
    )
    active_set = set(active)
    standby_ranked = [row for row in ranked if row.resolver not in active_set]
    standby = _select_diverse_resolvers(
        ranked_rows=standby_ranked,
        take=standby_n,
        max_per_prefix24=max_standby_per_prefix24,
    )
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


def _select_publish_rows(
    rows: list[ScanRow],
    publish_min_pass_rate: float,
    publish_max_latency_ms: float,
    publish_min_score: float,
) -> tuple[list[ScanRow], list[ScanRow]]:
    publish_rows = [
        row
        for row in rows
        if row.ok
        and row.pass_rate >= publish_min_pass_rate
        and (publish_max_latency_ms <= 0 or row.latency_ms <= publish_max_latency_ms)
        and row.score >= publish_min_score
    ]
    publish_set = {row.resolver for row in publish_rows}
    failed_rows = [row for row in rows if row.resolver not in publish_set]
    return publish_rows, failed_rows


def _build_incremental_report(
    args: argparse.Namespace,
    now_ts: float,
    rows: list[ScanRow],
    publish_rows: list[ScanRow],
    failed_rows: list[ScanRow],
    active: list[str],
    standby: list[str],
    quarantine: list[str],
    resolver_list: list[str],
    marked_publish_rows: list[ScanRow],
    marked_quarantine_rows: list[ScanRow],
    *,
    new_cursor: int,
    prefilter_cap: int,
    prefilter_count: int,
    prefilter_due: int,
    quick_rows: list[ScanRow],
    quick_probe_cap: int,
    quick_keep_cap: int,
    quick_selected_count: int,
    deep_probe_cap: int,
    deep_due: int,
    deep_target_count: int,
    runtime_state_file: str,
    runtime_reentry_due: int,
) -> dict[str, Any]:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "timestamp_ts": now_ts,
        "zone": args.zone,
        "total_scanned": len(rows),
        "deep_target_count": int(deep_target_count),
        "total_working": len(publish_rows),
        "scan_in_progress": True,
        "rollback_used": False,
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
            "prefilter_count": prefilter_count,
            "prefilter_due": int(prefilter_due),
            "quick_timeout": args.quick_timeout,
            "quick_concurrency": args.quick_concurrency,
            "quick_max_probe": quick_probe_cap,
            "quick_scanned": len(quick_rows),
            "quick_working": sum(1 for row in quick_rows if row.ok),
            "quick_selected": int(quick_selected_count),
            "quick_keep": quick_keep_cap,
            "deep_probe_cap": int(deep_probe_cap),
            "deep_due": int(deep_due),
            "deep_protocol_check": bool(args.deep_protocol_check),
            "protocol_roundtrips": int(args.protocol_roundtrips),
            "max_active_per_prefix24": int(args.max_active_per_prefix24),
            "max_standby_per_prefix24": int(args.max_standby_per_prefix24),
            "incremental_publish_sec": float(args.incremental_publish_sec),
            "runtime_state_file": runtime_state_file,
            "runtime_reentry_due": int(runtime_reentry_due),
        },
        "pools": {
            "active": active,
            "standby": standby,
            "quarantine": quarantine,
        },
        "resolvers": _rows_to_dicts(marked_publish_rows),
        "quarantine_resolvers": _rows_to_dicts(marked_quarantine_rows),
        "resolver_list": resolver_list,
    }


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
        remaining = probes - (idx + 1)
        # Fast-fail: when min_success is no longer reachable, stop probing early.
        if successes + remaining < need:
            break
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
    runtime_state = _load_runtime_state(args.runtime_state_json)
    cycle_now_ts = time.time()
    runtime_reentry_due = _promote_due_runtime_entries(runtime_state, cycle_now_ts)

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
    prefilter_due = sum(
        1
        for ip in prefilter_candidates
        if _is_probe_due_for_runtime(runtime_state.get(ip), cycle_now_ts)
    )
    prefilter_candidates = _prioritize_due_candidates(
        prefilter_candidates,
        runtime_state,
        cycle_now_ts,
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
    merged_unique = _dedupe_resolvers(merged_candidates)
    merged_due = sum(
        1
        for ip in merged_unique
        if _is_probe_due_for_runtime(runtime_state.get(ip), cycle_now_ts)
    )
    candidates = _prioritize_due_candidates(
        merged_unique,
        runtime_state,
        cycle_now_ts,
    )[:deep_probe_cap]
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
    pending: set[asyncio.Task[ScanRow]] = set(tasks)
    rows: list[ScanRow] = []
    incremental_publish_sec = max(0.0, float(args.incremental_publish_sec))
    last_incremental_publish_ts = time.time()

    while pending:
        if incremental_publish_sec > 0:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=incremental_publish_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            done, still_pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
        pending = set(still_pending)

        for task in done:
            rows.append(task.result())

        now_loop_ts = time.time()
        if (
            incremental_publish_sec > 0
            and pending
            and rows
            and (now_loop_ts - last_incremental_publish_ts) >= incremental_publish_sec
        ):
            partial_rows = sorted(rows, key=_row_rank_key)
            partial_publish, partial_failed = _select_publish_rows(
                partial_rows,
                publish_min_pass_rate=args.publish_min_pass_rate,
                publish_max_latency_ms=args.publish_max_latency_ms,
                publish_min_score=args.publish_min_score,
            )
            p_active, p_standby, p_quarantine = _classify_pools(
                publish_rows=partial_publish,
                failed_rows=partial_failed,
                active_pool_size=args.active_pool_size,
                standby_pool_size=args.standby_pool_size,
                max_active_per_prefix24=args.max_active_per_prefix24,
                max_standby_per_prefix24=args.max_standby_per_prefix24,
            )
            p_active_set = set(p_active)
            p_standby_set = set(p_standby)
            p_marked_publish = _mark_row_pools(partial_publish, p_active_set, p_standby_set)
            p_marked_quarantine = _mark_row_pools(partial_failed, set(), set())
            p_resolver_list = _build_resolver_list(
                active=p_active,
                standby=p_standby,
                publish_rows=partial_publish,
                previous_resolver_list=prev_resolver_list,
                source_candidates=source_candidates,
            )
            incremental_report = _build_incremental_report(
                args=args,
                now_ts=now_loop_ts,
                rows=partial_rows,
                publish_rows=partial_publish,
                failed_rows=partial_failed,
                active=p_active,
                standby=p_standby,
                quarantine=p_quarantine,
                resolver_list=p_resolver_list,
                marked_publish_rows=p_marked_publish,
                marked_quarantine_rows=p_marked_quarantine,
                new_cursor=new_cursor,
                prefilter_cap=prefilter_cap,
                prefilter_count=len(prefilter_candidates),
                prefilter_due=prefilter_due,
                quick_rows=quick_rows,
                quick_probe_cap=quick_probe_cap,
                quick_keep_cap=quick_keep_cap,
                quick_selected_count=len(quick_selected),
                deep_probe_cap=deep_probe_cap,
                deep_due=merged_due,
                deep_target_count=len(candidates),
                runtime_state_file=args.runtime_state_json,
                runtime_reentry_due=runtime_reentry_due,
            )
            _atomic_write_json(args.output, incremental_report)
            last_incremental_publish_ts = now_loop_ts

    rows.sort(key=_row_rank_key)
    publish_rows, failed_rows = _select_publish_rows(
        rows,
        publish_min_pass_rate=args.publish_min_pass_rate,
        publish_max_latency_ms=args.publish_max_latency_ms,
        publish_min_score=args.publish_min_score,
    )

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
        max_active_per_prefix24=args.max_active_per_prefix24,
        max_standby_per_prefix24=args.max_standby_per_prefix24,
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
    runtime_state, runtime_counters = _update_runtime_state(
        previous=runtime_state,
        rows=rows,
        active_set=active_set,
        standby_set=standby_set,
        now_ts=now_ts,
        cooldown_steps=getattr(args, "_reentry_cooldowns", _DEFAULT_REENTRY_COOLDOWN_SECONDS),
    )
    _atomic_write_json(
        args.runtime_state_json,
        _runtime_state_payload(runtime_state, now_ts),
    )
    runtime_counts = _runtime_state_counts(runtime_state)

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
            "prefilter_due": int(prefilter_due),
            "deep_due": int(merged_due),
            "global_pass_rate_avg": round(
                statistics.mean([row.pass_rate for row in rows]) if rows else 0.0,
                4,
            ),
            "deep_protocol_check": bool(args.deep_protocol_check),
            "protocol_roundtrips": int(args.protocol_roundtrips),
            "max_per_prefix24": int(args.max_per_prefix24),
            "max_active_per_prefix24": int(args.max_active_per_prefix24),
            "max_standby_per_prefix24": int(args.max_standby_per_prefix24),
            "incremental_publish_sec": float(args.incremental_publish_sec),
            "reentry_cooldown_seq_sec": list(
                getattr(args, "_reentry_cooldowns", _DEFAULT_REENTRY_COOLDOWN_SECONDS)
            ),
            "runtime_reentry_due": int(runtime_reentry_due),
            "runtime_state_file": args.runtime_state_json,
            "runtime_state_total": len(runtime_state),
            "runtime_state_counts": runtime_counts,
            "promotion_count": int(runtime_counters["promotion_count"]),
            "demotion_count": int(runtime_counters["demotion_count"]),
            "reentry_count": int(runtime_counters["reentry_count"]),
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
    parser.add_argument("--runtime-state-json", default="state/resolver_runtime_state.json")
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
    parser.add_argument("--max-active-per-prefix24", type=int, default=1, help="max active entries from same /24; <=0 disables")
    parser.add_argument("--max-standby-per-prefix24", type=int, default=2, help="max standby entries from same /24; <=0 disables")
    parser.add_argument("--incremental-publish-sec", type=float, default=8.0, help="publish partial scanner output during deep phase; <=0 disables")
    parser.add_argument("--reentry-cooldown-seq", default="30,60,120,300,600,1200", help="comma separated cooldown ladder in seconds")
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
    setattr(args, "_reentry_cooldowns", _parse_cooldown_sequence(args.reentry_cooldown_seq))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    return await run_loop(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
