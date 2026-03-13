"""
STORM Connection Module - Connection lifecycle and state management

Handles connection initiation, data buffering, and stream reassembly.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Callable, Optional

from storm_proto import (
    FrameType,
    PacketFlags,
    make_frame,
    make_packet,
    parse_frame,
    parse_packet,
)
from storm_fec import FECRecovery


class ConnectionState(str, Enum):
    INIT = "init"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"


@dataclass
class ConnectionConfig:
    """Configuration for a connection"""
    block_size: int = 16  # FEC block size
    keepalive_interval: float = 10.0  # Seconds
    keepalive_timeout: float = 5.0  # Seconds
    max_inflight: int = 64  # Max unacked packets
    reassembly_timeout: float = 30.0  # How long to hold packets for reordering


class STORMConnection:
    """Single STORM connection with resumable connection ID"""
    
    def __init__(
        self,
        config: Optional[ConnectionConfig] = None,
        conn_id: Optional[bytes] = None,
    ):
        self.config = config or ConnectionConfig()
        self.conn_id = conn_id or secrets.token_bytes(4)
        
        self.state = ConnectionState.INIT
        self.created_at = time.time()
        self.last_activity_at = time.time()
        
        # Outgoing sequence
        self._seq_out = 0
        self._seq_out_lock = Lock()
        
        # Incoming sequence reassembly
        self._seq_in = 0
        self._reassembly_buffer: dict[int, bytes] = {}  # seq -> data
        self._reassembly_lock = Lock()
        
        # FEC recovery
        self.fec = FECRecovery(block_size=self.config.block_size)
        
        # Buffering
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._recv_buffer: dict[int, bytes] = {}  # For ordered delivery
        
        # State
        self._keepalive_handle: Optional[asyncio.Task] = None
        self._closed_event = asyncio.Event()
    
    def conn_id_hex(self) -> str:
        return self.conn_id.hex()
    
    def uptime(self) -> float:
        return time.time() - self.created_at
    
    async def send_data(self, data: bytes) -> None:
        """Queue data for transmission"""
        # Current implementation does not have explicit handshake frames yet.
        # Open lazily on first send to keep transport path operational.
        if self.state == ConnectionState.INIT:
            self.state = ConnectionState.OPEN
        
        if self.state not in (ConnectionState.OPEN, ConnectionState.OPENING):
            raise RuntimeError(f"cannot send: state={self.state}")
        
        # Split into chunks and queue
        chunk_size = 200  # bytes per packet
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            await self._send_queue.put(chunk)
    
    def get_next_outgoing(self) -> Optional[bytes]:
        """
        Get next packet for transmission.
        Returns (packet_bytes, seq_number) or None if queue empty.
        """
        try:
            chunk = self._send_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        
        with self._seq_out_lock:
            seq = self._seq_out
            self._seq_out += 1
        
        flags = PacketFlags.DATA
        seq_group = seq // 256
        seq_offset = seq % 256
        frame = make_frame(
            frame_type=FrameType.DATA,
            frame_flags=0,
            seq_group=seq_group,
            payload=chunk,
        )
        
        packet = make_packet(
            conn_id=self.conn_id,
            flags=flags,
            seq_offset=seq_offset,
            payload=frame,
        )
        
        return packet
    
    async def handle_incoming_packet(self, packet_data: bytes) -> None:
        """Process incoming packet"""
        self.last_activity_at = time.time()
        
        try:
            header, payload = parse_packet(packet_data)
        except ValueError as e:
            print(f"[{self.conn_id_hex()}] parse error: {e}")
            return
        
        if header.flags & PacketFlags.KEEPALIVE:
            # Respond to keepalive
            pass  # Handled by caller
        elif header.flags & PacketFlags.DATA:
            # Regular data
            await self._handle_data_frame(header.seq_offset, payload)
        elif header.flags & PacketFlags.PARITY:
            # Parity/recovery
            await self._handle_parity_frame(payload)
    
    async def _handle_data_frame(self, seq_offset: int, payload: bytes) -> None:
        """Process data frame"""
        try:
            frame_header, frame_payload = parse_frame(payload)
        except ValueError:
            return
        
        if frame_header.frame_type == FrameType.DATA:
            absolute_seq = frame_header.seq_group * 256 + (seq_offset & 0xFF)
            
            with self._reassembly_lock:
                self._reassembly_buffer[absolute_seq] = frame_payload
            
            # Try FEC recovery if available
            recovered = self.fec.add_packet(absolute_seq, frame_payload)
            if recovered:
                print(f"[{self.conn_id_hex()}] FEC recovered packet {absolute_seq}")
        elif frame_header.frame_type in (FrameType.CLOSE, FrameType.RESET):
            self.state = ConnectionState.CLOSED
            self._closed_event.set()
    
    async def _handle_parity_frame(self, payload: bytes) -> None:
        """Process parity frame"""
        try:
            frame_header, parity_data = parse_frame(payload)
        except ValueError:
            return
        
        seq_base = frame_header.seq_group * 256
        recovered = self.fec.add_parity(seq_base, parity_data)
        if recovered:
            print(f"[{self.conn_id_hex()}] FEC recovered missing packet at {seq_base}")
    
    async def get_ordered_data(self, timeout: float = 1.0) -> Optional[bytes]:
        """
        Get next ordered data from reassembly buffer.
        May produce data out of order if loss not recoverable.
        """
        start = time.time()
        while time.time() - start < timeout:
            with self._reassembly_lock:
                if self._seq_in in self._reassembly_buffer:
                    data = self._reassembly_buffer.pop(self._seq_in)
                    self._seq_in += 1
                    return data
            
            await asyncio.sleep(0.01)
        
        return None
    
    async def close(self) -> None:
        """Close connection gracefully"""
        if self.state == ConnectionState.CLOSED:
            return
        
        self.state = ConnectionState.CLOSING
        
        # Send close frame
        close_frame = make_frame(
            frame_type=FrameType.CLOSE,
            frame_flags=0,
            seq_group=0,
            payload=b"",
        )
        close_packet = make_packet(
            conn_id=self.conn_id,
            flags=PacketFlags.DATA | PacketFlags.FINAL,
            seq_offset=255,
            payload=close_frame,
        )
        # Note: caller should handle actual transmission
        
        # Stop keepalive
        if self._keepalive_handle:
            self._keepalive_handle.cancel()
        
        self.state = ConnectionState.CLOSED
        self._closed_event.set()
    
    async def start_keepalive(
        self,
        send_fn: Callable[[bytes], None],
    ) -> None:
        """Start keepalive ping task"""
        async def keepalive_task():
            while self.state in (ConnectionState.OPEN, ConnectionState.OPENING):
                await asyncio.sleep(self.config.keepalive_interval)
                
                keepalive_packet = make_packet(
                    conn_id=self.conn_id,
                    flags=PacketFlags.KEEPALIVE,
                    seq_offset=0,
                    payload=b"",
                )
                send_fn(keepalive_packet)
        
        self._keepalive_handle = asyncio.create_task(keepalive_task())


class ConnectionManager:
    """Manages a pool of connections"""
    
    def __init__(self, config: Optional[ConnectionConfig] = None):
        self.config = config or ConnectionConfig()
        self._connections: dict[str, STORMConnection] = {}
        self._lock = Lock()
    
    def create_connection(
        self,
        conn_id: Optional[bytes] = None,
    ) -> STORMConnection:
        """Create a new connection"""
        conn = STORMConnection(config=self.config, conn_id=conn_id)
        
        with self._lock:
            self._connections[conn.conn_id_hex()] = conn
        
        return conn
    
    def get_connection(self, conn_id: bytes) -> Optional[STORMConnection]:
        """Get existing connection"""
        with self._lock:
            return self._connections.get(conn_id.hex())
    
    def remove_connection(self, conn_id: bytes) -> None:
        """Remove closed connection"""
        with self._lock:
            self._connections.pop(conn_id.hex(), None)
    
    def active_connections(self) -> list[STORMConnection]:
        """List active connections"""
        with self._lock:
            now = time.time()
            # Remove stale connections (no activity for 5+ minutes)
            stale = [
                cid for cid, conn in self._connections.items()
                if now - conn.last_activity_at > 300.0
            ]
            for cid in stale:
                del self._connections[cid]
            
            return list(self._connections.values())
