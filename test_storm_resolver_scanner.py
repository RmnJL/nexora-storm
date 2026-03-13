from __future__ import annotations

import json

from storm_resolver_scanner import (
    ScanRow,
    _build_resolver_list,
    _classify_pools,
    _dedupe_resolvers,
    _load_previous_report,
    _select_probe_candidates,
    _select_sticky_candidates,
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
    )
    assert active == ["1.1.1.1"]
    assert standby == ["8.8.8.8", "9.9.9.9"]
    assert quarantine == ["4.4.4.4"]


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
