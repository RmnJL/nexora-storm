from __future__ import annotations

from storm_resolver_daemon import (
    atomic_write_text,
    compute_selection,
    format_active_resolvers,
    parse_active_resolvers_text,
    select_probe_subset,
    should_restart,
    stabilize_selection,
)
from storm_resolver_picker import ProbeResult


def test_parse_active_resolvers_text_dedup():
    text = "1.1.1.1 8.8.8.8\n1.1.1.1\n"
    assert parse_active_resolvers_text(text) == ["1.1.1.1", "8.8.8.8"]


def test_stabilize_selection_avoids_order_flap():
    previous = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    selected = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
    assert stabilize_selection(previous, selected, take=3) == previous


def test_compute_selection_strict_mode_fails_on_low_health():
    results = [
        ProbeResult(resolver="1.1.1.1", ok=True, latency_ms=20.0),
        ProbeResult(resolver="8.8.8.8", ok=False, latency_ms=1000.0, error="no-response"),
    ]
    selected, healthy_count, eligible = compute_selection(
        results=results,
        take=2,
        min_healthy=2,
        allow_fallback=False,
    )
    assert selected == []
    assert healthy_count == 1
    assert not eligible


def test_should_restart_honors_cooldown():
    assert should_restart(last_restart_at=0.0, now=20.0, cooldown=10.0)
    assert not should_restart(last_restart_at=15.0, now=20.0, cooldown=10.0)


def test_atomic_write_text(tmp_path):
    target = tmp_path / "state" / "resolvers_active.txt"
    atomic_write_text(str(target), format_active_resolvers(["1.1.1.1", "8.8.8.8"]))
    assert target.read_text(encoding="utf-8") == "1.1.1.1 8.8.8.8\n"


def test_select_probe_subset_keeps_sticky_and_caps_size():
    pool = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4", "208.67.222.222"]
    prev = ["8.8.8.8", "9.9.9.9"]
    subset, cursor = select_probe_subset(
        pool=pool,
        previous_active=prev,
        max_probe=3,
        probe_mode="sequential",
        sticky_keep=2,
        cursor=0,
    )
    assert subset[:2] == ["8.8.8.8", "9.9.9.9"]
    assert len(subset) == 3
    assert cursor >= 0
