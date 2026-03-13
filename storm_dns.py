"""
STORM DNS Module - DNS query encoding/decoding and transmission

Wraps STORM packets into DNS queries for transport.
"""

from __future__ import annotations

import asyncio
import base64
import socket
from typing import Optional, Tuple

import dns.message
import dns.name
import dns.rdataclass
import dns.resolver


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
                    self._dns_query_async(q, resolver_ip),
                    timeout=timeout,
                )
                
                elapsed = (asyncio.get_event_loop().time() - start) * 1000
                
                # Extract response data
                resp_packet = self._extract_response(response)
                
                return resp_packet, elapsed
            
            except asyncio.TimeoutError:
                elapsed = (asyncio.get_event_loop().time() - start) * 1000
                return None, elapsed
        
        except Exception as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            print(f"DNS query error: {e}")
            return None, elapsed
    
    async def _dns_query_async(self, query: dns.message.Message, resolver_ip: str) -> dns.message.Message:
        """Send DNS query asynchronously"""
        loop = asyncio.get_event_loop()
        
        def blocking_query():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5.0)
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
    ) -> Tuple[Optional[bytes], str, float]:
        """
        Send query to any available resolver (parallel).
        Returns (response, used_resolver, latency_ms).
        """
        if not self.resolvers:
            return None, "", 0.0
        
        task_map = {
            asyncio.create_task(
                self.transport.send_query(packet, resolver, session_id, timeout)
            ): resolver
            for resolver in self.resolvers
        }
        pending = set(task_map.keys())
        last_resolver = self.resolvers[0]
        last_latency = 0.0
        
        # Race: return first successful response
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            
            for task in done:
                resolver = task_map[task]
                resp, latency = task.result()
                last_resolver = resolver
                last_latency = latency
                
                if resp is not None:
                    # Cancel pending
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    return resp, resolver, latency
        
        return None, last_resolver, last_latency
    
    async def send_to_specific(
        self,
        packet: bytes,
        resolver: str,
        session_id: str = "",
        timeout: float = 3.0,
    ) -> Tuple[Optional[bytes], float]:
        """Send query to specific resolver"""
        return await self.transport.send_query(packet, resolver, session_id, timeout)
