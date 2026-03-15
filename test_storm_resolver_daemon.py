from __future__ import annotations

import json

from storm_resolver_daemon import (
    atomic_write_text,
    compute_selection,
    format_active_resolvers,
    load_scanner_resolvers,
    _parse_allowed_pools,
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
        max_per_prefix24=2,
    )
    assert selected == []
    assert healthy_count == 1
    assert not eligible


def test_compute_selection_fills_to_take_with_fallback_ranked_results():
    results = [
        ProbeResult(resolver="1.1.1.1", ok=True, latency_ms=20.0),
        ProbeResult(resolver="8.8.8.8", ok=False, latency_ms=200.0, error="no-response"),
        ProbeResult(resolver="9.9.9.9", ok=False, latency_ms=400.0, error="no-response"),
    ]
    selected, healthy_count, eligible = compute_selection(
        results=results,
        take=3,
        min_healthy=1,
        allow_fallback=False,
        max_per_prefix24=2,
    )
    assert eligible
    assert healthy_count == 1
    assert len(selected) == 3
    assert selected[0] == "1.1.1.1"


def test_compute_selection_applies_prefix24_diversity():
    results = [
        ProbeResult(resolver="1.1.1.1", ok=True, latency_ms=20.0),
        ProbeResult(resolver="1.1.1.2", ok=True, latency_ms=21.0),
        ProbeResult(resolver="2.2.2.2", ok=True, latency_ms=22.0),
    ]
    selected, _, eligible = compute_selection(
        results=results,
        take=2,
        min_healthy=1,
        allow_fallback=False,
        max_per_prefix24=1,
    )
    assert eligible
    assert selected == ["1.1.1.1", "2.2.2.2"]


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


def test_load_scanner_resolvers_prefers_ranked_rows(tmp_path):
    path = tmp_path / "scan.json"
    payload = {
        "timestamp_ts": 9999999999.0,
        "resolvers": [
            {"resolver": "1.1.1.1", "pool": "active", "pass_rate": 0.9, "latency_ms": 30, "score": 10.0},
            {"resolver": "8.8.8.8", "pool": "quarantine", "pass_rate": 0.95, "latency_ms": 20, "score": 10.0},
        ],
        "resolver_list": ["9.9.9.9"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    selected = load_scanner_resolvers(
        scanner_json=str(path),
        min_pass_rate=0.5,
        max_latency_ms=200.0,
        min_score=0.0,
        max_stale_sec=0.0,
        allowed_pools=_parse_allowed_pools("active,standby"),
    )
    assert selected == ["1.1.1.1"]


def test_load_scanner_resolvers_falls_back_to_resolver_list(tmp_path):
    path = tmp_path / "scan.json"
    payload = {
        "timestamp_ts": 9999999999.0,
        "resolvers": [
            {"resolver": "8.8.8.8", "pool": "quarantine", "pass_rate": 0.4, "latency_ms": 999, "score": -1.0},
        ],
        "resolver_list": ["1.1.1.1", "9.9.9.9"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    selected = load_scanner_resolvers(
        scanner_json=str(path),
        min_pass_rate=0.6,
        max_latency_ms=200.0,
        min_score=0.0,
        max_stale_sec=0.0,
        allowed_pools=_parse_allowed_pools("active,standby"),
    )
    assert selected == ["1.1.1.1", "9.9.9.9"]
