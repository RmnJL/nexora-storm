from __future__ import annotations

import dns.message
import dns.rcode
import pytest

from storm_dns import DNSTransport
from storm_dns_server import STORMDNSServer
from storm_proto import PacketFlags, make_packet


class _DummySock:
    def __init__(self):
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
        self.sent.append((data, addr))
        return len(data)


def test_extract_storm_packet_rejects_non_storm_label():
    server = STORMDNSServer(tunnel_zone="t1.phonexpress.ir")
    query = dns.message.make_query("test.t1.phonexpress.ir", "TXT")
    assert server._extract_storm_packet(query) is None


def test_extract_storm_packet_accepts_valid_packet():
    server = STORMDNSServer(tunnel_zone="t1.phonexpress.ir")
    packet = make_packet(
        conn_id=b"abcd",
        flags=PacketFlags.KEEPALIVE,
        seq_offset=0,
        payload=b"",
    )
    qname = DNSTransport(zone="t1.phonexpress.ir", qtype="TXT").make_query_name(
        packet,
        session_id="probe",
    )
    query = dns.message.make_query(qname, "TXT")
    assert server._extract_storm_packet(query) == packet


@pytest.mark.asyncio
async def test_handle_query_sends_nxdomain_when_handler_returns_none():
    async def _handler(_packet: bytes, _addr: tuple[str, int]):
        return None

    server = STORMDNSServer(
        tunnel_zone="t1.phonexpress.ir",
        connection_handler=_handler,
    )
    packet = make_packet(
        conn_id=b"wxyz",
        flags=PacketFlags.KEEPALIVE,
        seq_offset=0,
        payload=b"",
    )
    qname = DNSTransport(zone="t1.phonexpress.ir", qtype="TXT").make_query_name(
        packet,
        session_id="probe",
    )
    query = dns.message.make_query(qname, "TXT")
    sock = _DummySock()

    await server._handle_query(query.to_wire(), ("127.0.0.1", 53000), sock)

    assert len(sock.sent) == 1
    response_wire, _ = sock.sent[0]
    response = dns.message.from_wire(response_wire)
    assert response.rcode() == dns.rcode.NXDOMAIN

