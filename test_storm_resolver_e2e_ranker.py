from __future__ import annotations

from storm_resolver_e2e_ranker import ResolverRankRow, parse_resolver_tokens, rank_rows, select_top


def test_parse_resolver_tokens_dedupes():
    got = parse_resolver_tokens("1.1.1.1 8.8.8.8\n1.1.1.1")
    assert got == ["1.1.1.1", "8.8.8.8"]


def test_rank_rows_prefers_success_rate_then_latency():
    rows = [
        ResolverRankRow("r1", 0.5, 500, 400, 4, 8, []),
        ResolverRankRow("r2", 0.75, 900, 800, 6, 8, []),
        ResolverRankRow("r3", 0.75, 450, 430, 6, 8, []),
    ]
    ranked = rank_rows(rows)
    assert [r.resolver for r in ranked] == ["r3", "r2", "r1"]


def test_select_top_falls_back_when_no_success():
    rows = [
        ResolverRankRow("r1", 0.0, 0, 0, 0, 8, []),
        ResolverRankRow("r2", 0.0, 0, 0, 0, 8, []),
    ]
    selected = select_top(rows, take=2)
    assert selected == ["r1", "r2"]
