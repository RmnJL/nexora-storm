from __future__ import annotations

import json

from storm_resolver_scanner import (
    ScanRow,
    _build_resolver_list,
    _classify_pools,
    _dedupe_resolvers,
    _load_previous_report,
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
