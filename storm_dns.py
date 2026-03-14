"""
STORM DNS Module - DNS query encoding/decoding and transmission

Wraps STORM packets into DNS queries for transport.
"""

from __future__ import annotations

import asyncio
import base64
import socket
from dataclasses import dataclass
from typing import Optional, Tuple

import dns.message
import dns.name
import dns.rdataclass
import dns.rcode
import dns.resolver


@dataclass(frozen=True)
class DNSQueryResult:
    resolver: str
    response_packet: Optional[bytes]
    latency_ms: float
    error_class: str = ""
    error_detail: str = ""
    rcode: int = -1
    attempted_resolvers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.response_packet is not None


class DNSTransport:
    """DNS query transport for STORM packets"""
    
    def __init__(self, zone: str = "tunnel.zone", qtype: str = "TXT"):
        self.zone = zone
        self.qtype = qtype
    
    @staticmethod
    def encode_packet(packet: bytes) -> str:
        """Encode packet to base32 for DNS label use"""
        # Use base32 without padding, lowercase
        encoded = base64.b32encode(packet).decode("ascii").lower().rstrip("=")
        return encoded
    
    @staticmethod
    def decode_packet(encoded: str) -> bytes:
        """Decode base32 DNS label back to packet"""
        # Add padding
        compact = encoded.replace(".", "").upper()
        pad = "=" * ((8 - (len(compact) % 8)) % 8)
        try:
            return base64.b32decode(compact + pad)
        except Exception:
            raise ValueError(f"bad base32: {encoded}")
    
    def make_query_name(self, packet: bytes, session_id: str = "") -> str:
        """Create DNS query name from packet"""
        encoded = self.encode_packet(packet)
        
        # Split into DNS-friendly labels (63 chars max each)
        labels = []
        for i in range(0, len(encoded), 50):
            labels.append(encoded[i:i+50])
        
        # Add session prefix if provided
        if session_id:
            labels.insert(0, session_id[:16])
        
        # Add zone suffix
        labels.append(self.zone)
        
        return ".".join(labels)
    
    async def send_query(
        self,
        packet: bytes,
        resolver_ip: str,
        session_id: str = "",
        timeout: float = 3.0,
    ) -> Tuple[Optional[bytes], float]:
        """
        Send DNS query with packet.
        Returns (response_packet, latency_ms) or (None, latency_ms) on timeout.
        """
        result = await self.send_query_detailed(
            packet=packet,
            resolver_ip=resolver_ip,
            session_id=session_id,
            timeout=timeout,
        )
        return result.response_packet, result.latency_ms

    async def send_query_detailed(
        self,
        packet: bytes,
        resolver_ip: str,
        session_id: str = "",
        timeout: float = 3.0,
    ) -> DNSQueryResult:
        """Send one query and return a classified result."""
        qname = self.make_query_name(packet, session_id)
        
        start = asyncio.get_event_loop().time()
        
        try:
            # Create DNS query
            q = dns.message.make_query(
                qname,
                self.qtype,
                dns.rdataclass.IN,
            )
            
            # Add EDNS0 for larger responses
            q.use_edns(edns=0, ednsflags=0, payload=4096)
            
            # Send query
            try:
                response = await asyncio.wait_for(
                    self._dns_query_async(q, resolver_ip, timeout=timeout),
                    timeout=max(0.2, timeout + 0.2),
                )
                
                elapsed = (asyncio.get_event_loop().time() - start) * 1000

                rcode = int(response.rcode())
                if rcode != dns.rcode.NOERROR:
                    return DNSQueryResult(
                        resolver=resolver_ip,
                        response_packet=None,
                        latency_ms=elapsed,
                        error_class=_rcode_to_error_class(rcode),
                        error_detail=dns.rcode.to_text(rcode),
                        rcode=rcode,
                    )
                
                # Extract response data
                resp_packet = self._extract_response(response)
                if resp_packet is None:
                    return DNSQueryResult(
                        resolver=resolver_ip,
                        response_packet=None,
                        latency_ms=elapsed,
                        error_class="empty-answer",
                        error_detail="no usable TXT payload",
                        rcode=rcode,
                    )

                return DNSQueryResult(
                    resolver=resolver_ip,
                    response_packet=resp_packet,
                    latency_ms=elapsed,
                    error_class="",
                    error_detail="",
                    rcode=rcode,
                )
            
            except asyncio.TimeoutError:
                elapsed = (asyncio.get_event_loop().time() - start) * 1000
                return DNSQueryResult(
                    resolver=resolver_ip,
                    response_packet=None,
                    latency_ms=elapsed,
                    error_class="timeout",
                    error_detail="query timeout",
                )
        
        except Exception as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            return DNSQueryResult(
                resolver=resolver_ip,
                response_packet=None,
                latency_ms=elapsed,
                error_class="query-error",
                error_detail=str(e),
            )
    
    async def _dns_query_async(
        self,
        query: dns.message.Message,
        resolver_ip: str,
        timeout: float = 3.0,
    ) -> dns.message.Message:
        """Send DNS query asynchronously"""
        loop = asyncio.get_event_loop()
        
        def blocking_query():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(max(0.2, float(timeout)))
            try:
                sock.sendto(query.to_wire(), (resolver_ip, 53))
                response_wire, _ = sock.recvfrom(4096)
                return dns.message.from_wire(response_wire)
            finally:
                sock.close()
        
        return await loop.run_in_executor(None, blocking_query)
    
    def _extract_response(self, response: dns.message.Message) -> Optional[bytes]:
        """Extract STORM packet from DNS response"""
        try:
            if response.answer:
                rrset = response.answer[0]
                for rr in rrset:
                    if self.qtype == "TXT":
                        # TXT record contains encoded packet
                        for txt_data in rr.strings:
                            txt_str = txt_data.decode("ascii", errors="ignore")
                            try:
                                return self.decode_packet(txt_str)
                            except ValueError:
                                continue
            
            return None
        except Exception:
            return None
    
    def make_response_packet(self, response_data: bytes) -> str:
        """Create DNS response containing STORM packet"""
        encoded = self.encode_packet(response_data)
        # Wrap in TXT record format (can use DNS library or manual)
        return encoded


class DNSGateway:
    """High-level DNS transport gateway with parallel resolvers"""
    
    def __init__(
        self,
        resolvers: list[str],
        zone: str = "tunnel.zone",
        qtype: str = "TXT",
    ):
        self.resolvers = resolvers
        self.transport = DNSTransport(zone=zone, qtype=qtype)
    
    async def send_to_any(
        self,
        packet: bytes,
        session_id: str = "",
        timeout: float = 3.0,
        resolver_order: Optional[list[str]] = None,
        fanout: int = 0,
    ) -> Tuple[Optional[bytes], str, float]:
        """
        Send query to any available resolver (parallel).
        Returns (response, used_resolver, latency_ms).
        """
        result = await self.send_to_any_detailed(
            packet=packet,
            session_id=session_id,
            timeout=timeout,
            resolver_order=resolver_order,
            fanout=fanout,
        )
        return result.response_packet, result.resolver, result.latency_ms

    async def send_to_any_detailed(
        self,
        packet: bytes,
        session_id: str = "",
        timeout: float = 3.0,
        resolver_order: Optional[list[str]] = None,
        fanout: int = 0,
    ) -> DNSQueryResult:
        ordered = _ordered_dedupe(resolver_order if resolver_order else self.resolvers)
        if not ordered:
            return DNSQueryResult(
                resolver="",
                response_packet=None,
                latency_ms=0.0,
                error_class="no-resolvers",
                error_detail="resolver list is empty",
            )

        batch_size = len(ordered) if int(fanout) <= 0 else min(max(1, int(fanout)), len(ordered))
        attempted: list[str] = []
        failures: list[DNSQueryResult] = []

        for start in range(0, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            attempted.extend(batch)
            task_map = {
                asyncio.create_task(
                    self.transport.send_query_detailed(
                        packet=packet,
                        resolver_ip=resolver,
                        session_id=session_id,
                        timeout=timeout,
                    )
                ): resolver
                for resolver in batch
            }
            pending = set(task_map.keys())

            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    result = task.result()
                    if result.ok:
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return DNSQueryResult(
                            resolver=result.resolver,
                            response_packet=result.response_packet,
                            latency_ms=result.latency_ms,
                            error_class=result.error_class,
                            error_detail=result.error_detail,
                            rcode=result.rcode,
                            attempted_resolvers=tuple(attempted),
                        )
                    failures.append(result)

            # Move to next batch if this batch fully failed.

        best = _pick_best_failure(failures)
        return DNSQueryResult(
            resolver=best.resolver if best else ordered[0],
            response_packet=None,
            latency_ms=best.latency_ms if best else timeout * 1000.0,
            error_class=best.error_class if best else "no-response",
            error_detail=best.error_detail if best else "all resolvers failed",
            rcode=best.rcode if best else -1,
            attempted_resolvers=tuple(attempted),
        )
    
    async def send_to_specific(
        self,
        packet: bytes,
        resolver: str,
        session_id: str = "",
        timeout: float = 3.0,
    ) -> Tuple[Optional[bytes], float]:
        """Send query to specific resolver"""
        return await self.transport.send_query(packet, resolver, session_id, timeout)


def _ordered_dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        resolver = str(raw).strip()
        if not resolver or resolver in seen:
            continue
        seen.add(resolver)
        out.append(resolver)
    return out


def _rcode_to_error_class(rcode: int) -> str:
    if rcode == dns.rcode.NXDOMAIN:
        return "nxdomain"
    if rcode == dns.rcode.SERVFAIL:
        return "servfail"
    if rcode == dns.rcode.REFUSED:
        return "refused"
    return "dns-error"


def _pick_best_failure(results: list[DNSQueryResult]) -> Optional[DNSQueryResult]:
    if not results:
        return None
    # Prefer explicit DNS/protocol failures over generic timeout when selecting reason.
    priority = {
        "conn-id-mismatch": 0,
        "empty-answer": 1,
        "nxdomain": 1,
        "servfail": 2,
        "refused": 2,
        "dns-error": 3,
        "query-error": 4,
        "timeout": 5,
        "no-response": 5,
    }
    return min(
        results,
        key=lambda r: (priority.get(r.error_class, 6), r.latency_ms),
    )
