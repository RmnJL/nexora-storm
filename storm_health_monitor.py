#!/usr/bin/env python3
"""
STORM health monitor and auto-failover helper.

Runs periodic SOCKS health probes against local storm-client, writes monitoring
reports, and triggers failover actions when health degrades for consecutive
cycles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

from storm_health_check import ProbeResult, compute_stats, run_socks5_probe

log = logging.getLogger("storm-health-monitor")


@dataclass
class MonitorState:
    fail_streak: int = 0
    last_action_ts: float = 0.0
    last_reason: str = ""


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


def parse_target_tokens(text: str, default_port: int) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for token in text.replace("\n", " ").split(" "):
        raw = token.strip()
        if not raw:
            continue
        host, port = _parse_target_endpoint(raw, default_port)
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _parse_target_endpoint(raw: str, default_port: int) -> tuple[str, int]:
    if ":" in raw:
        host, _, port_raw = raw.rpartition(":")
        host = host.strip()
        try:
            port = int(port_raw.strip())
        except ValueError:
            port = int(default_port)
        return host, max(1, min(65535, port))
    return raw.strip(), max(1, min(65535, int(default_port)))


def load_resolver_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_resolver_tokens(f.read())
    except FileNotFoundError:
        return []


def load_target_file(path: str, default_port: int) -> list[tuple[str, int]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_target_tokens(f.read(), default_port=default_port)
    except FileNotFoundError:
        return []


def atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_state(path: str) -> MonitorState:
    if not path or not os.path.isfile(path):
        return MonitorState()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return MonitorState(
            fail_streak=int(data.get("fail_streak", 0) or 0),
            last_action_ts=float(data.get("last_action_ts", 0.0) or 0.0),
            last_reason=str(data.get("last_reason", "") or ""),
        )
    except Exception:
        return MonitorState()


def build_failover_selection(active: list[str], healthy: list[str], take: int) -> list[str]:
    target = max(1, int(take))
    healthy_u = parse_resolver_tokens(" ".join(healthy))
    active_u = parse_resolver_tokens(" ".join(active))
    if not healthy_u:
        return active_u[:target]
    if not active_u:
        return healthy_u[:target]

    anchor = active_u[0]
    if anchor in healthy_u:
        idx = healthy_u.index(anchor)
        rotated = healthy_u[idx + 1 :] + healthy_u[: idx + 1]
    else:
        rotated = healthy_u

    selected = rotated[:target]
    if selected == active_u[:target] and len(healthy_u) > target:
        rotated = rotated[1:] + rotated[:1]
        selected = rotated[:target]
    return selected


def evaluate_health(stats: dict[str, Any], success_threshold: float, latency_threshold_ms: float) -> tuple[bool, str]:
    success_rate = float(stats.get("success_rate", 0.0) or 0.0)
    p95 = float(stats.get("latency_p95_ms", 0.0) or 0.0)
    reasons: list[str] = []
    if success_rate < success_threshold:
        reasons.append(
            f"success_rate={success_rate:.3f} < threshold={success_threshold:.3f}"
        )
    if p95 > latency_threshold_ms:
        reasons.append(
            f"p95={p95:.2f}ms > threshold={latency_threshold_ms:.2f}ms"
        )
    if reasons:
        return False, "; ".join(reasons)
    return True, "ok"


def run_cmdline(cmdline: str) -> tuple[bool, str]:
    if not cmdline.strip():
        return False, "empty cmd"
    try:
        cmd = shlex.split(cmdline)
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or "").strip()
        if not detail:
            detail = f"exit={proc.returncode}"
        return ok, detail
    except Exception as exc:
        return False, str(exc)


def run_restart_cmd(cmdline: str) -> tuple[bool, str]:
    return run_cmdline(cmdline)


def apply_failover_rotation(active_file: str, healthy_file: str, take: int) -> tuple[bool, list[str]]:
    active = load_resolver_file(active_file)
    healthy = load_resolver_file(healthy_file)
    selected = build_failover_selection(active=active, healthy=healthy, take=take)
    if not selected:
        return False, active
    if selected == active[: len(selected)]:
        return False, selected
    atomic_write_text(active_file, " ".join(selected) + "\n")
    return True, selected


async def run_probe_batch(args: argparse.Namespace) -> tuple[list[ProbeResult], dict[str, Any]]:
    results: list[ProbeResult] = []
    targets = _build_targets(args)
    target_log: list[str] = []
    for idx in range(args.checks):
        host, port = targets[idx % len(targets)]
        result = await run_socks5_probe(
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            target_host=host,
            target_port=port,
            timeout=args.timeout,
        )
        results.append(result)
        target_log.append(f"{host}:{port}")
        if idx < args.checks - 1 and args.interval > 0:
            await asyncio.sleep(args.interval)
    stats = compute_stats(results)
    stats["targets_used"] = sorted(set(target_log))
    return results, stats


def _build_targets(args: argparse.Namespace) -> list[tuple[str, int]]:
    merged: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    cli_tokens = " ".join(args.targets or [])
    for item in parse_target_tokens(cli_tokens, default_port=args.target_port):
        if item not in seen:
            seen.add(item)
            merged.append(item)

    if args.targets_file:
        for item in load_target_file(args.targets_file, default_port=args.target_port):
            if item not in seen:
                seen.add(item)
                merged.append(item)

    if merged:
        return merged
    return [(args.target_host, args.target_port)]


async def run_loop(args: argparse.Namespace) -> int:
    state = load_state(args.state_json)
    while True:
        started = time.time()
        now = time.time()
        action: dict[str, Any] = {
            "triggered": False,
            "rotated": False,
            "restart_ok": False,
            "restart_detail": "",
            "selected": [],
            "rank_attempted": False,
            "rank_ok": False,
            "rank_detail": "",
            "rank_selected": [],
        }

        results, stats = await run_probe_batch(args)
        healthy, reason = evaluate_health(
            stats=stats,
            success_threshold=args.success_threshold,
            latency_threshold_ms=args.latency_threshold_ms,
        )
        if healthy:
            state.fail_streak = 0
            state.last_reason = "ok"
        else:
            state.fail_streak += 1
            state.last_reason = reason

        if not healthy and state.fail_streak >= args.fail_streak_trigger:
            if (now - state.last_action_ts) >= args.action_cooldown:
                action["triggered"] = True
                rank_eligible = (
                    bool(args.e2e_rank_cmd.strip())
                    and state.fail_streak >= max(1, int(args.e2e_rank_min_fail_streak))
                )
                if rank_eligible:
                    action["rank_attempted"] = True
                    rank_ok, rank_detail = run_cmdline(args.e2e_rank_cmd)
                    action["rank_ok"] = rank_ok
                    action["rank_detail"] = rank_detail
                    action["rank_selected"] = load_resolver_file(args.active_file)[: max(1, int(args.take))]
                    if action["rank_ok"] and not action["rank_selected"]:
                        action["rank_ok"] = False
                        action["rank_detail"] = (
                            f"{rank_detail}; no resolvers written to active file"
                            if rank_detail
                            else "no resolvers written to active file"
                        )
                    action["selected"] = action["rank_selected"]

                if bool(args.failover_rotate):
                    # If e2e ranking failed, fall back to simple healthy-list rotation.
                    if not action["rank_ok"]:
                        rotated, selected = apply_failover_rotation(
                            active_file=args.active_file,
                            healthy_file=args.healthy_file,
                            take=args.take,
                        )
                        action["rotated"] = rotated
                        action["selected"] = selected

                restart_ok, restart_detail = run_restart_cmd(args.failover_restart_cmd)
                action["restart_ok"] = restart_ok
                action["restart_detail"] = restart_detail
                state.last_action_ts = now

        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "timestamp_ts": now,
            "probe": {
                "proxy": f"{args.proxy_host}:{args.proxy_port}",
                "target": f"{args.target_host}:{args.target_port}",
                "targets": [f"{h}:{p}" for h, p in _build_targets(args)],
                "checks": args.checks,
                "interval": args.interval,
                "timeout": args.timeout,
            },
            "stats": stats,
            "result_ok": healthy,
            "reason": reason,
            "state": asdict(state),
            "action": action,
            "recent_checks": [asdict(r) for r in results[-10:]],
        }
        atomic_write_json(args.report_json, report)
        atomic_write_json(args.state_json, asdict(state))

        log.info(
            "health ok=%s rate=%.3f p95=%.1f fail_streak=%d action=%s",
            healthy,
            float(stats.get("success_rate", 0.0) or 0.0),
            float(stats.get("latency_p95_ms", 0.0) or 0.0),
            state.fail_streak,
            "triggered" if action["triggered"] else "none",
        )
        if action["triggered"]:
            log.warning(
                "failover action rotated=%s restart_ok=%s detail=%s selected=%s reason=%s",
                action["rotated"],
                action["restart_ok"],
                action["restart_detail"],
                " ".join(action["selected"]),
                reason,
            )

        if args.loop <= 0:
            return 0

        elapsed = time.time() - started
        await asyncio.sleep(max(1.0, args.loop - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STORM health monitor + failover watchdog")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=1443)
    parser.add_argument("--target-host", default="1.1.1.1")
    parser.add_argument("--target-port", type=int, default=443)
    parser.add_argument("--targets", nargs="*", default=[], help="optional list of target endpoints host[:port] for round-robin probe")
    parser.add_argument("--targets-file", default="", help="optional file containing target endpoints")
    parser.add_argument("--checks", type=int, default=6)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--success-threshold", type=float, default=0.50)
    parser.add_argument("--latency-threshold-ms", type=float, default=5000.0)
    parser.add_argument("--loop", type=float, default=30.0, help="seconds between monitor cycles; <=0 means run once")

    parser.add_argument("--active-file", default="state/resolvers_active.txt")
    parser.add_argument("--healthy-file", default="state/resolvers_healthy.txt")
    parser.add_argument("--take", type=int, default=4)
    parser.add_argument("--fail-streak-trigger", type=int, default=3)
    parser.add_argument("--action-cooldown", type=float, default=180.0)
    parser.add_argument("--failover-rotate", type=int, default=1)
    parser.add_argument("--failover-restart-cmd", default="systemctl restart storm-client")
    parser.add_argument("--e2e-rank-min-fail-streak", type=int, default=6)
    parser.add_argument("--e2e-rank-cmd", default="")

    parser.add_argument("--state-json", default="state/health_monitor_state.json")
    parser.add_argument("--report-json", default="state/health_monitor_report.json")
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
