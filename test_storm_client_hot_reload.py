from __future__ import annotations

from storm_client import load_resolvers_file, parse_resolver_tokens
from storm_dns import DNSGateway


def test_parse_resolver_tokens_filters_private_and_invalid():
    raw = "1.1.1.1 127.0.0.1 bad 8.8.8.8 10.0.0.1"
    assert parse_resolver_tokens(raw) == ["1.1.1.1", "8.8.8.8"]


def test_load_resolvers_file_applies_min_and_max(tmp_path):
    path = tmp_path / "active.txt"
    path.write_text("1.1.1.1 8.8.8.8 9.9.9.9", encoding="utf-8")
    assert load_resolvers_file(str(path), min_selected=2, max_selected=2) == [
        "1.1.1.1",
        "8.8.8.8",
    ]
    assert load_resolvers_file(str(path), min_selected=4, max_selected=8) == []


def test_dns_gateway_set_resolvers_hot_reload():
    gw = DNSGateway(["1.1.1.1"], zone="t1.example.test", qtype="TXT")
    changed = gw.set_resolvers(["8.8.8.8", "9.9.9.9"])
    assert changed is True
    assert gw.get_resolvers() == ["8.8.8.8", "9.9.9.9"]
