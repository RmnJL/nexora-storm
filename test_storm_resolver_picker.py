from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from storm_resolver_picker import (
    ProbeResult,
    _parse_resolver_lines,
    choose_probe_candidates,
    main,
    rank_resolvers,
)


def test_parse_resolver_lines_filters_invalid_and_comments():
    lines = [
        "# comment",
        "",
        "1.1.1.1",
        "8.8.8.8   # inline note",
        "not-an-ip",
        "1.1.1.1",
    ]
    assert _parse_resolver_lines(lines) == ["1.1.1.1", "8.8.8.8"]


def test_rank_resolvers_prefers_healthy_then_latency():
    results = [
        ProbeResult(resolver="9.9.9.9", ok=False, latency_ms=10.0, error="no-response"),
        ProbeResult(resolver="8.8.8.8", ok=True, latency_ms=120.0),
        ProbeResult(resolver="1.1.1.1", ok=True, latency_ms=80.0),
    ]
    assert rank_resolvers(results, take=2) == ["1.1.1.1", "8.8.8.8"]


def test_choose_probe_candidates_head_mode():
    resolvers = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert choose_probe_candidates(resolvers, max_probe=2, sample_mode="head") == ["1.1.1.1", "8.8.8.8"]


def test_choose_probe_candidates_random_mode_size():
    resolvers = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"]
    picked = choose_probe_candidates(resolvers, max_probe=3, sample_mode="random")
    assert len(picked) == 3
    assert len(set(picked)) == 3
    assert set(picked).issubset(set(resolvers))


def test_main_writes_json_even_on_threshold_failure(monkeypatch, tmp_path):
    out_path = tmp_path / "probe.json"
    args = SimpleNamespace(
        resolvers_file="dummy.txt",
        zone="t1.phonexpress.ir",
        qtype="TXT",
        timeout=1.0,
        max_probe=1,
        concurrency=1,
        sample_mode="head",
        take=1,
        min_healthy=1,
        allow_fallback=False,
        env_out="",
        json_out=str(out_path),
    )

    monkeypatch.setattr("storm_resolver_picker.parse_args", lambda: args)
    monkeypatch.setattr("storm_resolver_picker.load_resolvers_file", lambda _path: ["1.1.1.1"])

    async def _fake_probe_pool(**_kwargs):
        return [ProbeResult(resolver="1.1.1.1", ok=False, latency_ms=10.0, error="no-response")]

    monkeypatch.setattr("storm_resolver_picker.probe_pool", _fake_probe_pool)

    rc = asyncio.run(main())
    assert rc == 2
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["healthy_count"] == 0
    assert payload["eligible"] is False
