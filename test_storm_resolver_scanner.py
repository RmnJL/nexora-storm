from __future__ import annotations

import json

from storm_resolver_scanner import (
    ResolverRuntimeState,
    ScanRow,
    _atomic_write_json,
    _build_resolver_list,
    _build_prefilter_candidates,
    _classify_pools,
    _cooldown_for_fail_streak,
    _dedupe_resolvers,
    _parse_cooldown_sequence,
    _load_runtime_state,
    _load_previous_report,
    _prioritize_due_candidates,
    _promote_due_runtime_entries,
    _resolve_phase1_limits,
    _runtime_state_payload,
    _select_quick_survivors,
    _select_probe_candidates,
    _select_sticky_candidates,
    _update_runtime_state,
)


def test_dedupe_resolvers_keeps_public_ipv4_only():
    items = ["1.1.1.1", "1.1.1.1", "8.8.8.8", "127.0.0.1", "not-ip"]
    assert _dedupe_resolvers(items) == ["1.1.1.1", "8.8.8.8"]


def test_classify_pools_splits_active_and_standby():
    publish = [
        ScanRow("1.1.1.1", True, 2, 2, 1.0, 20.0, 980.0, ""),
        ScanRow("8.8.8.8", True, 2, 2, 1.0, 30.0, 970.0, ""),
        ScanRow("9.9.9.9", True, 2, 2, 1.0, 40.0, 960.0, ""),
    ]
    failed = [ScanRow("4.4.4.4", False, 0, 2, 0.0, 1500.0, -500.0, "no-response")]
    active, standby, quarantine = _classify_pools(
        publish_rows=publish,
        failed_rows=failed,
        active_pool_size=1,
        standby_pool_size=2,
        max_per_prefix24=2,
    )
    assert active == ["1.1.1.1"]
    assert standby == ["8.8.8.8", "9.9.9.9"]
    assert quarantine == ["4.4.4.4"]


def test_classify_pools_applies_prefix24_diversity():
    publish = [
        ScanRow("1.1.1.1", True, 2, 2, 1.0, 20.0, 980.0, ""),
        ScanRow("1.1.1.2", True, 2, 2, 1.0, 21.0, 979.0, ""),
        ScanRow("2.2.2.2", True, 2, 2, 1.0, 22.0, 978.0, ""),
    ]
    active, standby, _ = _classify_pools(
        publish_rows=publish,
        failed_rows=[],
        active_pool_size=2,
        standby_pool_size=0,
        max_per_prefix24=1,
    )
    assert active == ["1.1.1.1", "2.2.2.2"]
    assert standby == []


def test_build_resolver_list_preserves_priority_order():
    publish = [
        ScanRow("1.1.1.1", True, 2, 2, 1.0, 20.0, 980.0, "", "active"),
        ScanRow("8.8.8.8", True, 2, 2, 1.0, 30.0, 970.0, "", "standby"),
    ]
    out = _build_resolver_list(
        active=["1.1.1.1"],
        standby=["8.8.8.8"],
        publish_rows=publish,
        previous_resolver_list=["9.9.9.9"],
        source_candidates=["4.4.4.4", "1.1.1.1"],
    )
    assert out[:4] == ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"]


def test_load_previous_report_parses_rows_and_resolver_list(tmp_path):
    path = tmp_path / "scan.json"
    payload = {
        "timestamp_ts": 123.0,
        "resolvers": [
            {
                "resolver": "1.1.1.1",
                "ok": True,
                "successes": 2,
                "probes": 2,
                "pass_rate": 1.0,
                "latency_ms": 20.0,
                "score": 980.0,
                "error": "",
                "pool": "active",
            }
        ],
        "resolver_list": ["1.1.1.1", "8.8.8.8"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    rows, resolver_list, ts = _load_previous_report(str(path))
    assert len(rows) == 1
    assert rows[0].resolver == "1.1.1.1"
    assert resolver_list == ["1.1.1.1", "8.8.8.8"]
    assert ts == 123.0


def test_load_previous_report_filters_quarantine_rows(tmp_path):
    path = tmp_path / "scan.json"
    payload = {
        "timestamp_ts": 456.0,
        "resolvers": [
            {
                "resolver": "1.1.1.1",
                "ok": True,
                "successes": 2,
                "probes": 2,
                "pass_rate": 1.0,
                "latency_ms": 20.0,
                "score": 980.0,
                "error": "",
                "pool": "active",
            },
            {
                "resolver": "8.8.8.8",
                "ok": False,
                "successes": 0,
                "probes": 2,
                "pass_rate": 0.0,
                "latency_ms": 1500.0,
                "score": -500.0,
                "error": "no-response",
                "pool": "quarantine",
            },
        ],
        "resolver_list": ["1.1.1.1", "8.8.8.8"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    rows, resolver_list, ts = _load_previous_report(str(path))
    assert len(rows) == 1
    assert rows[0].resolver == "1.1.1.1"
    assert resolver_list == ["1.1.1.1", "8.8.8.8"]
    assert ts == 456.0


def test_select_probe_candidates_sequential_rotates_cursor():
    src = ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5"]
    picks1, cursor1 = _select_probe_candidates(
        source_candidates=src,
        max_probe=2,
        sample_mode="sequential",
        cursor=0,
    )
    picks2, cursor2 = _select_probe_candidates(
        source_candidates=src,
        max_probe=2,
        sample_mode="sequential",
        cursor=cursor1,
    )
    assert picks1 == ["1.1.1.1", "2.2.2.2"]
    assert picks2 == ["3.3.3.3", "4.4.4.4"]
    assert cursor2 == 4


def test_select_sticky_candidates_uses_previous_active_and_standby():
    previous = [
        ScanRow("9.9.9.9", True, 2, 2, 1.0, 25.0, 975.0, "", "active"),
        ScanRow("8.8.8.8", True, 2, 2, 1.0, 35.0, 965.0, "", "standby"),
        ScanRow("4.4.4.4", True, 2, 2, 1.0, 45.0, 955.0, "", "quarantine"),
    ]
    sticky = _select_sticky_candidates(
        previous_rows=previous,
        source_candidates=["8.8.8.8", "9.9.9.9", "1.1.1.1"],
        sticky_rows=4,
        sticky_pools={"active", "standby"},
    )
    assert sticky == ["9.9.9.9", "8.8.8.8"]


def test_resolve_phase1_limits_auto_uses_deep_probe_as_floor():
    deep_cap, prefilter_cap, quick_probe_cap, quick_keep_cap = _resolve_phase1_limits(
        max_probe=300,
        prefilter_keep=0,
        quick_max_probe=0,
        quick_keep=0,
    )
    assert deep_cap == 300
    assert prefilter_cap >= 1000
    assert quick_probe_cap >= deep_cap
    assert quick_probe_cap <= prefilter_cap
    assert quick_keep_cap == deep_cap


def test_build_prefilter_candidates_prioritizes_sticky_then_history():
    previous = [
        ScanRow("9.9.9.9", True, 2, 2, 1.0, 30.0, 970.0, "", "active"),
        ScanRow("8.8.8.8", True, 2, 2, 1.0, 40.0, 960.0, "", "standby"),
    ]
    out = _build_prefilter_candidates(
        source_candidates=["1.1.1.1", "8.8.8.8", "4.4.4.4"],
        sampled_candidates=["4.4.4.4"],
        sticky_candidates=["8.8.8.8"],
        previous_rows=previous,
        previous_resolver_list=["7.7.7.7"],
        prefilter_keep=5,
        prefilter_history_rows=2,
    )
    assert out == ["8.8.8.8", "9.9.9.9", "4.4.4.4", "7.7.7.7", "1.1.1.1"]


def test_select_quick_survivors_prefers_healthy_rows():
    rows = [
        ScanRow("1.1.1.1", False, 0, 1, 0.0, 1200.0, -200.0, "no-response"),
        ScanRow("8.8.8.8", True, 1, 1, 1.0, 200.0, 800.0, ""),
        ScanRow("9.9.9.9", True, 1, 1, 1.0, 150.0, 850.0, ""),
    ]
    out = _select_quick_survivors(rows, keep=2)
    assert out == ["9.9.9.9", "8.8.8.8"]


def test_select_quick_survivors_falls_back_when_everything_fails():
    rows = [
        ScanRow("1.1.1.1", False, 0, 1, 0.0, 400.0, -400.0, "no-response"),
        ScanRow("8.8.8.8", False, 0, 1, 0.0, 700.0, -700.0, "no-response"),
    ]
    out = _select_quick_survivors(rows, keep=1)
    assert out == ["1.1.1.1"]


def test_runtime_state_roundtrip_load_and_parse(tmp_path):
    path = tmp_path / "resolver_runtime_state.json"
    now_ts = 1234.0
    runtime = {
        "1.1.1.1": ResolverRuntimeState(
            resolver="1.1.1.1",
            fail_streak=2,
            success_streak=0,
            last_seen_ok_ts=1000.0,
            last_seen_fail_ts=1100.0,
            next_probe_at_ts=1200.0,
            sr_short=0.25,
            sr_long=0.40,
            state="quarantine",
        )
    }
    _atomic_write_json(str(path), _runtime_state_payload(runtime, now_ts))
    loaded = _load_runtime_state(str(path))
    assert "1.1.1.1" in loaded
    item = loaded["1.1.1.1"]
    assert item.fail_streak == 2
    assert item.state == "quarantine"
    assert item.sr_short == 0.25


def test_update_runtime_state_promote_and_demote_counts():
    prev = {
        "1.1.1.1": ResolverRuntimeState(
            resolver="1.1.1.1",
            fail_streak=2,
            success_streak=0,
            sr_short=0.0,
            sr_long=0.0,
            state="quarantine",
        ),
        "8.8.8.8": ResolverRuntimeState(
            resolver="8.8.8.8",
            fail_streak=0,
            success_streak=4,
            sr_short=1.0,
            sr_long=1.0,
            state="active",
        ),
    }
    rows = [
        ScanRow("1.1.1.1", True, 2, 2, 1.0, 100.0, 900.0, ""),
        ScanRow("8.8.8.8", False, 0, 2, 0.0, 1500.0, -500.0, "no-response"),
    ]
    updated, counters = _update_runtime_state(
        previous=prev,
        rows=rows,
        active_set={"1.1.1.1"},
        standby_set=set(),
        now_ts=2000.0,
        cooldown_steps=(30.0, 60.0, 120.0, 300.0, 600.0, 1200.0),
    )
    assert updated["1.1.1.1"].state == "active"
    assert updated["1.1.1.1"].fail_streak == 0
    assert updated["8.8.8.8"].state == "degraded"
    assert updated["8.8.8.8"].fail_streak == 1
    assert updated["8.8.8.8"].next_probe_at_ts == 2030.0
    assert counters["promotion_count"] == 1
    assert counters["demotion_count"] == 1


def test_update_runtime_state_reentry_to_probation_when_not_in_pool():
    prev = {
        "9.9.9.9": ResolverRuntimeState(
            resolver="9.9.9.9",
            fail_streak=4,
            success_streak=0,
            sr_short=0.0,
            sr_long=0.1,
            state="quarantine",
        )
    }
    rows = [ScanRow("9.9.9.9", True, 1, 2, 0.5, 400.0, 100.0, "")]
    updated, counters = _update_runtime_state(
        previous=prev,
        rows=rows,
        active_set=set(),
        standby_set=set(),
        now_ts=3000.0,
        cooldown_steps=(30.0, 60.0, 120.0, 300.0, 600.0, 1200.0),
    )
    assert updated["9.9.9.9"].state == "probation"
    assert updated["9.9.9.9"].success_streak == 1
    assert counters["reentry_count"] == 1


def test_parse_cooldown_sequence_uses_default_for_invalid_input():
    values = _parse_cooldown_sequence("x,,")
    assert values == (30.0, 60.0, 120.0, 300.0, 600.0, 1200.0)


def test_cooldown_for_fail_streak_uses_ladder_cap():
    steps = (30.0, 60.0, 120.0)
    assert _cooldown_for_fail_streak(1, steps) == 30.0
    assert _cooldown_for_fail_streak(2, steps) == 60.0
    assert _cooldown_for_fail_streak(3, steps) == 120.0
    assert _cooldown_for_fail_streak(7, steps) == 120.0


def test_promote_due_runtime_entries_moves_quarantine_and_degraded():
    runtime = {
        "1.1.1.1": ResolverRuntimeState(
            resolver="1.1.1.1",
            state="quarantine",
            next_probe_at_ts=100.0,
        ),
        "8.8.8.8": ResolverRuntimeState(
            resolver="8.8.8.8",
            state="degraded",
            next_probe_at_ts=150.0,
        ),
        "9.9.9.9": ResolverRuntimeState(
            resolver="9.9.9.9",
            state="quarantine",
            next_probe_at_ts=250.0,
        ),
    }
    moved = _promote_due_runtime_entries(runtime, now_ts=200.0)
    assert moved == 2
    assert runtime["1.1.1.1"].state == "probation"
    assert runtime["8.8.8.8"].state == "probation"
    assert runtime["9.9.9.9"].state == "quarantine"


def test_prioritize_due_candidates_keeps_due_first():
    runtime = {
        "1.1.1.1": ResolverRuntimeState(
            resolver="1.1.1.1",
            state="quarantine",
            next_probe_at_ts=500.0,
        ),
        "8.8.8.8": ResolverRuntimeState(
            resolver="8.8.8.8",
            state="candidate",
            next_probe_at_ts=0.0,
        ),
    }
    ordered = _prioritize_due_candidates(["1.1.1.1", "8.8.8.8", "9.9.9.9"], runtime, now_ts=100.0)
    assert ordered == ["8.8.8.8", "9.9.9.9", "1.1.1.1"]
