from __future__ import annotations

from storm_health_check import ProbeResult, _build_connect_request, compute_stats


def test_build_connect_request_ipv4():
    req = _build_connect_request("1.2.3.4", 1080)
    assert req[:4] == b"\x05\x01\x00\x01"
    assert req[-2:] == (1080).to_bytes(2, "big")


def test_build_connect_request_domain():
    req = _build_connect_request("example.com", 443)
    assert req[:4] == b"\x05\x01\x00\x03"
    domain_len = req[4]
    assert req[5 : 5 + domain_len] == b"example.com"
    assert req[-2:] == (443).to_bytes(2, "big")


def test_compute_stats_success_and_failures():
    results = [
        ProbeResult(ok=True, latency_ms=100.0, error=None, timestamp=1.0),
        ProbeResult(ok=True, latency_ms=200.0, error=None, timestamp=2.0),
        ProbeResult(ok=False, latency_ms=5000.0, error="timeout", timestamp=3.0),
    ]
    stats = compute_stats(results)
    
    assert stats["total"] == 3
    assert stats["success"] == 2
    assert stats["failures"] == 1
    assert abs(stats["success_rate"] - (2 / 3)) < 1e-9
    assert stats["latency_min_ms"] == 100.0
    assert stats["latency_max_ms"] == 200.0
    assert "timeout" in stats["errors"][0]
