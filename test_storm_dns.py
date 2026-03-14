from __future__ import annotations

from storm_dns import DNSQueryResult, _ordered_dedupe, _pick_best_failure, _rcode_to_error_class


def test_ordered_dedupe_keeps_order():
    assert _ordered_dedupe(["8.8.8.8", "1.1.1.1", "8.8.8.8", ""]) == ["8.8.8.8", "1.1.1.1"]


def test_rcode_mapping_is_explicit():
    assert _rcode_to_error_class(3) == "nxdomain"
    assert _rcode_to_error_class(2) == "servfail"
    assert _rcode_to_error_class(5) == "refused"


def test_pick_best_failure_prefers_protocol_errors_over_timeout():
    failures = [
        DNSQueryResult(resolver="1.1.1.1", response_packet=None, latency_ms=1200.0, error_class="timeout"),
        DNSQueryResult(resolver="8.8.8.8", response_packet=None, latency_ms=400.0, error_class="empty-answer"),
    ]
    picked = _pick_best_failure(failures)
    assert picked is not None
    assert picked.resolver == "8.8.8.8"
