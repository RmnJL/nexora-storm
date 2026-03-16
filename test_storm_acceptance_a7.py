from __future__ import annotations

import types

from storm_acceptance_a7 import Snapshot, evaluate_acceptance, parse_resolver_tokens


def _args() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        min_success_rate=0.15,
        min_success_ratio=0.60,
        max_flaps_per_hour=12.0,
        require_reentry_count=1,
    )


def test_parse_resolver_tokens_dedupes():
    assert parse_resolver_tokens("1.1.1.1 8.8.8.8\n1.1.1.1") == ["1.1.1.1", "8.8.8.8"]


def test_evaluate_acceptance_passes_with_expected_signals():
    snaps = [
        Snapshot(
            ts=0.0,
            active=["1.1.1.1", "8.8.8.8"],
            health_success_rate=0.2,
            scanner_reentry_count=1,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
        Snapshot(
            ts=1800.0,
            active=["1.1.1.1", "8.8.8.8"],
            health_success_rate=0.18,
            scanner_reentry_count=0,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
        Snapshot(
            ts=3600.0,
            active=["1.1.1.1", "8.8.8.8"],
            health_success_rate=0.16,
            scanner_reentry_count=0,
            monitor_reentry_ok=True,
            monitor_reentry_selected_count=1,
        ),
    ]
    out = evaluate_acceptance(_args(), snaps)
    assert out["ok"] is True
    assert out["checks"]["health_ok"] is True
    assert out["checks"]["reentry_ok"] is True
    assert out["summary"]["flaps"] == 0


def test_evaluate_acceptance_fails_when_active_becomes_empty():
    snaps = [
        Snapshot(
            ts=0.0,
            active=["1.1.1.1"],
            health_success_rate=0.2,
            scanner_reentry_count=0,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
        Snapshot(
            ts=100.0,
            active=[],
            health_success_rate=0.2,
            scanner_reentry_count=0,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
    ]
    out = evaluate_acceptance(_args(), snaps)
    assert out["ok"] is False
    assert out["checks"]["active_nonempty_ok"] is False


def test_evaluate_acceptance_fails_when_health_not_stable():
    snaps = [
        Snapshot(
            ts=0.0,
            active=["1.1.1.1"],
            health_success_rate=0.1,
            scanner_reentry_count=2,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
        Snapshot(
            ts=3600.0,
            active=["1.1.1.1"],
            health_success_rate=0.1,
            scanner_reentry_count=0,
            monitor_reentry_ok=False,
            monitor_reentry_selected_count=0,
        ),
    ]
    out = evaluate_acceptance(_args(), snaps)
    assert out["ok"] is False
    assert out["checks"]["health_ok"] is False

