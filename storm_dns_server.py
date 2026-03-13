"""
STORM DNS Server Implementation - Real DNS listener using dnspython

Handles incoming DNS queries and routes STORM packets to the connection manager.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional, Tuple

import dns.message
import dns.name
import dns.rdatatype
import dns.rdataclass
import dns.rdata
import dns.rrset
import dns.rcode
import dns.flags

from storm_proto import parse_packet

log = logging.getLogger("storm-dns-server")


class STORMDNSServer:
    """
    Real DNS server that handles STORM protocol queries.
    
    Extracts STORM packets from DNS query names and routes them
    to the connection manager.
    """
    
    def __init__(
        self,
        listen_host: str = "0.0.0.0",
        listen_port: int = 53,
        tunnel_zone: str = "t1.phonexpress.ir",
        connection_handler=None,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.tunnel_zone = tunnel_zone
        self.tunnel_zone_obj = dns.name.from_text(tunnel_zone)
        self.connection_handler = connection_handler
        self.running = False
    
    async def start(self) -> None:
        """Start DNS server"""
        log.info(f"STORM DNS server starting on {self.listen_host}:{self.listen_port}")
        self.running = True
        
        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.listen_host, self.listen_port))
        sock.setblocking(False)
        
        loop = asyncio.get_event_loop()
        
        try:
            while self.running:
                try:
                    # Receive query
                    data, addr = sock.recvfrom(4096)
                    
                    # Process in background
                    asyncio.create_task(
                        self._handle_query(data, addr, sock)
                    )
                
                except BlockingIOError:
                    # No data available, yield
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error(f"receive error: {e}")
                    await asyncio.sleep(0.1)
        
        finally:
            sock.close()
            log.info("DNS server stopped")
    
    async def _handle_query(
        self,
        query_data: bytes,
        client_addr: Tuple[str, int],
        sock: socket.socket,
    ) -> None:
        """Handle incoming DNS query"""
        try:
            # Parse query
            query = dns.message.from_wire(query_data)
            
            log.debug(f"query from {client_addr}: {query.question[0].name} {dns.rdatatype.to_text(query.question[0].rdtype)}")
            
            # Check if this is a STORM query
            storm_packet = self._extract_storm_packet(query)
            
            if storm_packet:
                # Process STORM packet
                if self.connection_handler:
                    response_data = await self.connection_handler(storm_packet, client_addr)
                    if response_data:
                        # Send response
                        response = self._make_response(query, response_data)
                        response_wire = response.to_wire()
                        sock.sendto(response_wire, client_addr)
                        log.debug(f"sent response to {client_addr}: {len(response_wire)} bytes")
                    else:
                        # Invalid or rejected STORM packet should still get DNS reply.
                        response = self._make_nxdomain(query)
                        sock.sendto(response.to_wire(), client_addr)
                else:
                    # No handler, send NXDOMAIN
                    response = self._make_nxdomain(query)
                    sock.sendto(response.to_wire(), client_addr)
            else:
                # Not a STORM query, handle as normal DNS
                response = self._make_nxdomain(query)
                sock.sendto(response.to_wire(), client_addr)
        
        except Exception as e:
            log.error(f"query handling error: {e}")
        except asyncio.CancelledError:
            raise
    
    def _extract_storm_packet(self, query: dns.message.Message) -> Optional[bytes]:
        """Extract STORM packet from DNS query name"""
        if not query.question:
            return None
        
        qname = query.question[0].name
        qtype = query.question[0].rdtype
        
        # Check if query is for our tunnel zone
        if not qname.is_subdomain(self.tunnel_zone_obj):
            return None
        
        try:
            # Extract labels before tunnel zone
            labels = list(qname)[:-len(list(self.tunnel_zone_obj))]
            
            if not labels:
                return None
            
            # Format: [session_id].[encoded_packet].tunnel.zone
            # `dns.name.Name` labels are raw bytes; decode safely.
            labels_text = [
                label.decode("ascii", errors="ignore")
                if isinstance(label, (bytes, bytearray))
                else str(label)
                for label in labels
            ]
            candidates = [".".join(labels_text)]
            
            # Client may prepend session_id label; retry without first label.
            if len(labels_text) > 1:
                candidates.append(".".join(labels_text[1:]))
            
            # Decode from base32
            from storm_dns import DNSTransport
            for encoded_str in candidates:
                try:
                    decoded = DNSTransport.decode_packet(encoded_str)
                    # Accept only valid STORM wire packets. This avoids
                    # accidental base32-looking labels (e.g. "test") causing
                    # silent no-response paths.
                    parse_packet(decoded)
                    return decoded
                except ValueError:
                    continue
                except Exception:
                    continue
            return None
        
        except Exception as e:
            log.debug(f"packet extraction error: {e}")
            return None
    
    def _make_response(
        self,
        query: dns.message.Message,
        response_packet: bytes,
    ) -> dns.message.Message:
        """Create DNS response with STORM packet"""
        response = dns.message.make_response(query)
        
        try:
            # Encode packet as TXT record
            from storm_dns import DNSTransport
            encoded = DNSTransport.encode_packet(response_packet)
            
            # Create TXT record
            rrset = dns.rrset.RRset(
                query.question[0].name,
                dns.rdataclass.IN,
                dns.rdatatype.TXT,
            )
            
            # Split into 255-byte chunks (TXT record limit)
            chunks = [encoded[i:i+255] for i in range(0, len(encoded), 255)]
            txt_text = " ".join(f"\"{chunk}\"" for chunk in (chunks or [""]))
            rr = dns.rdata.from_text(
                dns.rdataclass.IN,
                dns.rdatatype.TXT,
                txt_text,
            )
            rrset.add(rr, 300)
            
            response.answer.append(rrset)
        
        except Exception as e:
            log.warning(f"response encoding error: {e}")
        
        return response
    
    def _make_nxdomain(self, query: dns.message.Message) -> dns.message.Message:
        """Create NXDOMAIN response"""
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.NXDOMAIN)
        return response


async def demo_dns_server():
    """Demo DNS server with mock handler"""
    
    # Mock connection handler
    async def handle_storm_packet(packet: bytes, addr: Tuple[str, int]) -> Optional[bytes]:
        """Mock handler that echoes packet back"""
        log.info(f"Received STORM packet: {len(packet)} bytes from {addr[0]}")
        
        try:
            # Try to parse
            header, payload = parse_packet(packet)
            log.info(f"  conn_id: {header.conn_id.hex()}")
            log.info(f"  flags: {header.flags:02x}")
            log.info(f"  payload: {len(payload)} bytes")
        except Exception as e:
            log.error(f"  parse error: {e}")
        
        # Echo back (in real use, would process and route to target)
        return packet
    
    server = STORMDNSServer(
        listen_host="127.0.0.1",
        listen_port=5353,  # Use non-standard port
        tunnel_zone="t1.phonexpress.ir",
        connection_handler=handle_storm_packet,
    )
    
    try:
        await server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.running = False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    print("STORM DNS Server Demo")
    print("Listening on 127.0.0.1:5353")
    print("Press Ctrl+C to stop\n")
    
    asyncio.run(demo_dns_server())
