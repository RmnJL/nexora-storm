from __future__ import annotations

import json

from storm_health_monitor import (
    build_failover_selection,
    evaluate_health,
    is_active_weak,
    load_reentry_candidates,
    parse_resolver_tokens,
    parse_target_tokens,
)


def test_parse_resolver_tokens_dedupes_and_ignores_empty():
    text = "1.1.1.1  \n8.8.8.8 1.1.1.1\n"
    assert parse_resolver_tokens(text) == ["1.1.1.1", "8.8.8.8"]


def test_build_failover_selection_rotates_away_from_current_anchor():
    active = ["1.1.1.1", "8.8.8.8"]
    healthy = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"]
    selected = build_failover_selection(active=active, healthy=healthy, take=2)
    assert selected == ["8.8.8.8", "9.9.9.9"]


def test_build_failover_selection_falls_back_to_active_when_healthy_empty():
    active = ["1.1.1.1", "8.8.8.8"]
    healthy: list[str] = []
    selected = build_failover_selection(active=active, healthy=healthy, take=2)
    assert selected == active


def test_evaluate_health_passes_when_thresholds_are_met():
    stats = {"success_rate": 0.8, "latency_p95_ms": 400.0}
    ok, reason = evaluate_health(stats, success_threshold=0.5, latency_threshold_ms=1000.0)
    assert ok is True
    assert reason == "ok"


def test_evaluate_health_fails_for_low_success_and_high_latency():
    stats = {"success_rate": 0.2, "latency_p95_ms": 6000.0}
    ok, reason = evaluate_health(stats, success_threshold=0.5, latency_threshold_ms=1000.0)
    assert ok is False
    assert "success_rate=" in reason
    assert "p95=" in reason


def test_parse_target_tokens_accepts_host_and_host_port():
    targets = parse_target_tokens("1.1.1.1:443 8.8.8.8 example.com", default_port=8443)
    assert targets == [("1.1.1.1", 443), ("8.8.8.8", 8443), ("example.com", 8443)]


def test_load_reentry_candidates_prefers_standby_then_quarantine(tmp_path):
    path = tmp_path / "scan.json"
    payload = {
        "pools": {
            "standby": ["1.1.1.1", "8.8.8.8"],
            "quarantine": ["9.9.9.9"],
        },
        "quarantine_resolvers": [
            {"resolver": "4.4.4.4"},
            {"resolver": "9.9.9.9"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = load_reentry_candidates(
        scanner_json=str(path),
        standby_max=2,
        quarantine_max=3,
        max_total=4,
    )
    assert out == ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"]


def test_is_active_weak_uses_min_floor_threshold():
    assert is_active_weak(active=["1.1.1.1"], take=4, min_active=2)
    assert not is_active_weak(active=["1.1.1.1", "8.8.8.8"], take=4, min_active=2)
