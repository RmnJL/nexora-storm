from __future__ import annotations

from storm_resolver_picker import ProbeResult, _parse_resolver_lines, rank_resolvers


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

