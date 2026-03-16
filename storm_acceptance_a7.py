#!/usr/bin/env python3
"""
Phase A7 acceptance runner for inside STORM stack.

It samples inside state files for a fixed window (default 60 minutes) and
evaluates acceptance criteria:
1) resolvers_active.txt should not become empty.
2) at least one re-entry event should be observed.
3) active-set flapping should stay below threshold.
4) health success-rate should be stably above threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any


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


def load_resolver_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_resolver_tokens(f.read())
    except FileNotFoundError:
        return []


def load_json_file(path: str) -> dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json_num(data: dict[str, Any], keys: list[str], default: float = 0.0) -> float:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    try:
        return float(cur)
    except Exception:
        return default


def _json_bool(data: dict[str, Any], keys: list[str], default: bool = False) -> bool:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return bool(cur)


def _json_list_len(data: dict[str, Any], keys: list[str]) -> int:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return 0
        cur = cur.get(key)
    if isinstance(cur, list):
        return len(cur)
    return 0


@dataclass
class Snapshot:
    ts: float
    active: list[str]
    health_success_rate: float | None
    scanner_reentry_count: int
    monitor_reentry_ok: bool
    monitor_reentry_selected_count: int


def read_snapshot(args: argparse.Namespace) -> Snapshot:
    active = load_resolver_file(args.active_file)
    scanner = load_json_file(args.scanner_json)
    health = load_json_file(args.health_report_json)

    rate_raw = _json_num(health, ["stats", "success_rate"], default=-1.0)
    health_rate = rate_raw if rate_raw >= 0.0 else None

    scanner_reentry = int(
        _json_num(scanner, ["observability", "reentry_count"], default=-1.0)
    )
    if scanner_reentry < 0:
        scanner_reentry = int(_json_num(scanner, ["metrics", "reentry_count"], default=0.0))

    monitor_reentry_ok = _json_bool(health, ["action", "reentry_ok"], default=False)
    monitor_reentry_selected_count = _json_list_len(health, ["action", "reentry_selected"])

    return Snapshot(
        ts=time.time(),
        active=active,
        health_success_rate=health_rate,
        scanner_reentry_count=max(0, scanner_reentry),
        monitor_reentry_ok=monitor_reentry_ok,
        monitor_reentry_selected_count=max(0, monitor_reentry_selected_count),
    )


def _count_flaps(snapshots: list[Snapshot]) -> int:
    flaps = 0
    for idx in range(1, len(snapshots)):
        if snapshots[idx].active != snapshots[idx - 1].active:
            flaps += 1
    return flaps


def _count_reentry_events(snapshots: list[Snapshot]) -> int:
    total = 0
    for snap in snapshots:
        total += int(snap.scanner_reentry_count)
        if snap.monitor_reentry_ok:
            total += 1
        elif snap.monitor_reentry_selected_count > 0:
            total += 1
    return total


def evaluate_acceptance(args: argparse.Namespace, snapshots: list[Snapshot]) -> dict[str, Any]:
    if not snapshots:
        return {
            "ok": False,
            "reason": "no snapshots collected",
            "checks": {},
            "summary": {},
        }

    start_ts = snapshots[0].ts
    end_ts = snapshots[-1].ts
    elapsed = max(1.0, end_ts - start_ts)
    flaps = _count_flaps(snapshots)
    flaps_per_hour = flaps * 3600.0 / elapsed

    empty_active_samples = sum(1 for snap in snapshots if len(snap.active) == 0)
    active_nonempty_ok = empty_active_samples == 0

    health_values = [snap.health_success_rate for snap in snapshots if snap.health_success_rate is not None]
    if health_values:
        health_pass_samples = sum(1 for value in health_values if value >= args.min_success_rate)
        health_pass_ratio = health_pass_samples / len(health_values)
        health_last = float(health_values[-1])
    else:
        health_pass_ratio = 0.0
        health_last = 0.0
    health_ok = (
        len(health_values) > 0
        and health_pass_ratio >= args.min_success_ratio
        and health_last >= args.min_success_rate
    )

    reentry_events = _count_reentry_events(snapshots)
    reentry_ok = reentry_events >= args.require_reentry_count

    flap_ok = flaps_per_hour <= args.max_flaps_per_hour

    checks = {
        "active_nonempty_ok": active_nonempty_ok,
        "reentry_ok": reentry_ok,
        "flap_ok": flap_ok,
        "health_ok": health_ok,
    }
    ok = all(checks.values())

    summary = {
        "samples": len(snapshots),
        "elapsed_sec": elapsed,
        "flaps": flaps,
        "flaps_per_hour": round(flaps_per_hour, 3),
        "empty_active_samples": empty_active_samples,
        "health_samples": len(health_values),
        "health_pass_ratio": round(health_pass_ratio, 4),
        "health_last_success_rate": round(health_last, 4),
        "reentry_events": reentry_events,
    }

    reasons: list[str] = []
    if not active_nonempty_ok:
        reasons.append(f"active-empty-samples={empty_active_samples}")
    if not reentry_ok:
        reasons.append(
            f"reentry-events={reentry_events} < required={args.require_reentry_count}"
        )
    if not flap_ok:
        reasons.append(
            f"flaps-per-hour={flaps_per_hour:.2f} > threshold={args.max_flaps_per_hour:.2f}"
        )
    if not health_ok:
        reasons.append(
            f"health-pass-ratio={health_pass_ratio:.3f} last={health_last:.3f} "
            f"(need ratio>={args.min_success_ratio:.3f}, last>={args.min_success_rate:.3f})"
        )

    return {
        "ok": ok,
        "reason": "ok" if ok else "; ".join(reasons),
        "checks": checks,
        "summary": summary,
        "thresholds": {
            "min_success_rate": args.min_success_rate,
            "min_success_ratio": args.min_success_ratio,
            "max_flaps_per_hour": args.max_flaps_per_hour,
            "require_reentry_count": args.require_reentry_count,
        },
    }


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase A7 acceptance runner for inside STORM")
    p.add_argument("--active-file", default="state/resolvers_active.txt")
    p.add_argument("--scanner-json", default="state/resolvers_scan.json")
    p.add_argument("--health-report-json", default="state/health_monitor_report.json")
    p.add_argument("--duration-sec", type=float, default=3600.0)
    p.add_argument("--sample-interval-sec", type=float, default=30.0)
    p.add_argument("--min-success-rate", type=float, default=0.15)
    p.add_argument("--min-success-ratio", type=float, default=0.60)
    p.add_argument("--max-flaps-per-hour", type=float, default=12.0)
    p.add_argument("--require-reentry-count", type=int, default=1)
    p.add_argument("--json-out", default="state/a7_acceptance_report.json")
    return p.parse_args()


def _collect_samples(args: argparse.Namespace) -> list[Snapshot]:
    samples: list[Snapshot] = []
    duration = max(0.0, float(args.duration_sec))
    interval = max(1.0, float(args.sample_interval_sec))
    started = time.time()

    while True:
        now = time.time()
        samples.append(read_snapshot(args))
        if (now - started) >= duration:
            break
        remaining = duration - (now - started)
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    return samples


def main() -> int:
    args = parse_args()
    snapshots = _collect_samples(args)
    evaluation = evaluate_acceptance(args, snapshots)
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "timestamp_ts": time.time(),
        "inputs": {
            "active_file": args.active_file,
            "scanner_json": args.scanner_json,
            "health_report_json": args.health_report_json,
            "duration_sec": args.duration_sec,
            "sample_interval_sec": args.sample_interval_sec,
        },
        "result": evaluation,
        "snapshots": [asdict(s) for s in snapshots],
    }
    atomic_write_json(args.json_out, payload)

    summary = evaluation.get("summary", {})
    print(
        "A7 ACCEPTANCE "
        f"{'PASS' if evaluation.get('ok') else 'FAIL'} "
        f"samples={summary.get('samples', 0)} "
        f"health_ratio={summary.get('health_pass_ratio', 0.0)} "
        f"flaps_per_hour={summary.get('flaps_per_hour', 0.0)} "
        f"reentry_events={summary.get('reentry_events', 0)}"
    )
    if not evaluation.get("ok"):
        print(f"reason: {evaluation.get('reason', '')}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

