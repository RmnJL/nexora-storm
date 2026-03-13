"""
End-to-end integration test for STORM DNS tunnel path.

Validates bidirectional TCP payload transfer over:
  local TCP client -> STORMClient -> DNS/UDP -> STORMServer -> target TCP echo
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress

import dns.message
import pytest

from storm_client import STORMClient
from storm_dns import DNSTransport
from storm_server import STORMServer


def _get_free_port(family: int, sock_type: int) -> int:
    with socket.socket(family, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_free_tcp_port() -> int:
    return _get_free_port(socket.AF_INET, socket.SOCK_STREAM)


def _get_free_udp_port() -> int:
    return _get_free_port(socket.AF_INET, socket.SOCK_DGRAM)


@pytest.mark.asyncio
async def test_storm_tcp_roundtrip_over_dns(monkeypatch: pytest.MonkeyPatch):
    dns_port = _get_free_udp_port()
    target_port = _get_free_tcp_port()
    client_port = _get_free_tcp_port()
    zone = "test.storm.local"
    
    # Force DNSTransport test queries to hit our local STORM DNS listener.
    async def _dns_query_async_local(self, query: dns.message.Message, resolver_ip: str):
        loop = asyncio.get_running_loop()
        
        def _blocking_query():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            try:
                sock.sendto(query.to_wire(), ("127.0.0.1", dns_port))
                wire, _ = sock.recvfrom(4096)
                return dns.message.from_wire(wire)
            finally:
                sock.close()
        
        return await loop.run_in_executor(None, _blocking_query)
    
    monkeypatch.setattr(DNSTransport, "_dns_query_async", _dns_query_async_local)
    
    async def _echo_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
    
    echo_server = await asyncio.start_server(_echo_handler, "127.0.0.1", target_port)
    
    storm_server = STORMServer(
        listen_host="127.0.0.1",
        listen_port=dns_port,
        target_host="127.0.0.1",
        target_port=target_port,
        tunnel_zone=zone,
    )
    storm_client = STORMClient(
        resolvers=["127.0.0.1"],
        listen_host="127.0.0.1",
        listen_port=client_port,
        zone=zone,
        qtype="TXT",
        poll_interval=0.05,
    )
    
    server_task = asyncio.create_task(storm_server.start())
    client_task = asyncio.create_task(storm_client.start())
    
    try:
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", client_port)
        
        payload = b"storm-end-to-end-check"
        writer.write(payload)
        await writer.drain()
        
        received = await asyncio.wait_for(
            reader.readexactly(len(payload)),
            timeout=5.0,
        )
        assert received == payload
        
        writer.close()
        await writer.wait_closed()
    
    finally:
        storm_client.running = False
        storm_server.running = False
        if storm_server.dns_server is not None:
            storm_server.dns_server.running = False
        
        for task in (client_task, server_task):
            task.cancel()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(client_task, server_task, return_exceptions=True),
                timeout=3.0,
            )
        
        echo_server.close()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(echo_server.wait_closed(), timeout=1.0)
