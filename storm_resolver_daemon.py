#!/usr/bin/env python3
"""
Background resolver manager for STORM inside gateway.

Continuously probes resolver pool and maintains an active resolver set file.
Optionally restarts storm-client when active resolver set changes.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
import os
import random
import shlex
import subprocess
import time
from typing import Any

from storm_resolver_picker import ProbeResult, load_resolvers_file, probe_pool, rank_resolvers

log = logging.getLogger("storm-resolver-daemon")


def parse_active_resolvers_text(text: str) -> list[str]:
    tokens = [token.strip() for token in text.replace("\n", " ").split(" ") if token.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def load_active_resolvers(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_active_resolvers_text(f.read())
    except FileNotFoundError:
        return []


def format_active_resolvers(resolvers: list[str]) -> str:
    return " ".join(resolvers).strip() + "\n"


def atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)


def stabilize_selection(previous: list[str], selected: list[str], take: int) -> list[str]:
    target_len = max(1, take)
    selected_set = set(selected)
    stable: list[str] = []

    # Keep existing active resolvers first when still healthy to avoid flapping.
    for resolver in previous:
        if resolver in selected_set and resolver not in stable:
            stable.append(resolver)
            if len(stable) >= target_len:
                return stable

    for resolver in selected:
        if resolver not in stable:
            stable.append(resolver)
            if len(stable) >= target_len:
                return stable

    return stable[:target_len]


def compute_selection(
    results: list[ProbeResult],
    take: int,
    min_healthy: int,
    allow_fallback: bool,
) -> tuple[list[str], int, bool]:
    healthy = [r for r in results if r.ok]
    healthy_count = len(healthy)

    if healthy_count < max(1, min_healthy) and not allow_fallback:
        return [], healthy_count, False

    basis = healthy if healthy else results
    return rank_resolvers(basis, take), healthy_count, True


def should_restart(last_restart_at: float, now: float, cooldown: float) -> bool:
    if cooldown <= 0:
        return True
    return (now - last_restart_at) >= cooldown


def _unique_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def select_probe_subset(
    pool: list[str],
    previous_active: list[str],
    max_probe: int,
    probe_mode: str,
    sticky_keep: int,
    cursor: int,
) -> tuple[list[str], int]:
    unique_pool = _unique_ordered(pool)
    if not unique_pool:
        return [], cursor

    cap = min(max(1, max_probe), len(unique_pool))
    sticky_cap = max(0, sticky_keep)
    sticky = [r for r in previous_active if r in unique_pool][:sticky_cap]
    sticky = _unique_ordered(sticky)

    selected: list[str] = list(sticky)
    if len(selected) >= cap:
        return selected[:cap], cursor

    remaining = [r for r in unique_pool if r not in set(selected)]
    need = cap - len(selected)
    if need <= 0 or not remaining:
        return selected, cursor

    if probe_mode == "random":
        picks = random.sample(remaining, need) if need < len(remaining) else remaining
        return selected + picks, cursor

    if probe_mode == "sequential":
        start = cursor % len(remaining)
        rotated = remaining[start:] + remaining[:start]
        picks = rotated[:need]
        new_cursor = (start + len(picks)) % len(remaining)
        return selected + picks, new_cursor

    raise ValueError(f"unsupported probe_mode: {probe_mode}")


class ResolverDaemon:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.last_restart_at = 0.0
        self.probe_cursor = 0

    async def run_once(self) -> dict[str, Any]:
        pool = load_resolvers_file(self.args.resolvers_file)
        if not pool:
            raise RuntimeError("no valid resolvers in resolver file")

        previous = load_active_resolvers(self.args.active_out)
        probe_set, self.probe_cursor = select_probe_subset(
            pool=pool,
            previous_active=previous,
            max_probe=self.args.max_probe,
            probe_mode=self.args.probe_mode,
            sticky_keep=self.args.sticky_keep,
            cursor=self.probe_cursor,
        )

        results = await probe_pool(
            resolvers=probe_set,
            zone=self.args.zone,
            qtype=self.args.qtype,
            timeout=self.args.timeout,
            max_probe=len(probe_set),
            concurrency=self.args.concurrency,
            sample_mode="head",
        )
        selected, healthy_count, eligible = compute_selection(
            results=results,
            take=self.args.take,
            min_healthy=self.args.min_healthy,
            allow_fallback=self.args.allow_fallback,
        )

        healthy_ranked = rank_resolvers([r for r in results if r.ok], self.args.healthy_out_max)
        if healthy_ranked:
            atomic_write_text(self.args.healthy_out, format_active_resolvers(healthy_ranked))

        changed = False
        restarted = False
        restart_reason = ""

        if eligible:
            stabilized = stabilize_selection(previous, selected, self.args.take)
            changed = stabilized != previous
            atomic_write_text(self.args.active_out, format_active_resolvers(stabilized))

            now = time.time()
            if changed and self.args.restart_on_change:
                if should_restart(self.last_restart_at, now, self.args.restart_cooldown):
                    restarted, restart_reason = self._restart_client()
                    if restarted:
                        self.last_restart_at = now
                else:
                    restart_reason = "cooldown"
        else:
            log.warning(
                "resolver health below threshold: healthy=%d min=%d fallback=%s",
                healthy_count,
                self.args.min_healthy,
                self.args.allow_fallback,
            )

        state = {
            "timestamp": time.time(),
            "pool_size": len(pool),
            "probed_count": len(probe_set),
            "probe_mode": self.args.probe_mode,
            "healthy_count": healthy_count,
            "eligible": eligible,
            "previous": previous,
            "selected": selected,
            "active": load_active_resolvers(self.args.active_out),
            "healthy_ranked": healthy_ranked,
            "changed": changed,
            "restarted": restarted,
            "restart_reason": restart_reason,
            "results": [asdict(r) for r in results],
        }
        atomic_write_text(self.args.state_json, json.dumps(state, ensure_ascii=False, indent=2))

        log.info(
            "cycle: pool=%d probed=%d healthy=%d eligible=%s changed=%s restarted=%s active=%s",
            len(pool),
            len(probe_set),
            healthy_count,
            eligible,
            changed,
            restarted,
            " ".join(state["active"]),
        )
        return state

    def _restart_client(self) -> tuple[bool, str]:
        cmd = shlex.split(self.args.restart_cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.args.restart_timeout,
            )
            if result.returncode == 0:
                return True, "ok"

            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            msg = stderr or stdout or f"exit={result.returncode}"
            log.error("restart command failed: %s", msg)
            return False, msg
        except Exception as exc:
            log.error("restart command exception: %s", exc)
            return False, str(exc)

    async def run_forever(self) -> None:
        while True:
            started = time.time()
            try:
                await self.run_once()
            except Exception:
                log.exception("resolver daemon cycle failed")

            elapsed = time.time() - started
            sleep_for = max(1.0, self.args.interval - elapsed)
            await asyncio.sleep(sleep_for)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STORM resolver background daemon")
    parser.add_argument("--resolvers-file", default="data/resolvers.txt")
    parser.add_argument("--active-out", default="state/resolvers_active.txt")
    parser.add_argument("--state-json", default="state/resolver_daemon_state.json")
    parser.add_argument("--zone", default="t1.phonexpress.ir")
    parser.add_argument("--qtype", default="TXT")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--max-probe", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=15)
    parser.add_argument("--probe-mode", choices=["random", "sequential"], default="random")
    parser.add_argument("--sticky-keep", type=int, default=4)
    parser.add_argument("--take", type=int, default=4)
    parser.add_argument("--min-healthy", type=int, default=2)
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--healthy-out", default="state/resolvers_healthy.txt")
    parser.add_argument("--healthy-out-max", type=int, default=256)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--restart-on-change", action="store_true")
    parser.add_argument("--restart-cmd", default="systemctl restart storm-client")
    parser.add_argument("--restart-timeout", type=float, default=20.0)
    parser.add_argument("--restart-cooldown", type=float, default=600.0)
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    daemon = ResolverDaemon(args)

    if args.run_once:
        try:
            await daemon.run_once()
            return 0
        except Exception as exc:
            log.error("run-once failed: %s", exc)
            return 2

    await daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
