from __future__ import annotations

from storm_health_monitor import (
    build_failover_selection,
    evaluate_health,
    parse_resolver_tokens,
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
