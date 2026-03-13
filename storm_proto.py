"""
STORM Protocol - Stateful Tunnel Over Resilient Messaging

Core primitives for resilient DNS-based connection layer.
Signature: Rmn JL
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Protocol magic and version
MAGIC = b"STRM"
VERSION = 1

# Packet flags
class PacketFlags(int, Enum):
    PARITY = 0x01  # This is a parity/recovery packet
    RETRANSMIT = 0x02  # Sender is retransmitting a known lost packet
    KEEPALIVE = 0x04  # Keepalive ping
    KEEPALIVE_ACK = 0x08  # Keepalive acknowledgement
    FINAL = 0x10  # Final packet in stream
    DATA = 0x20  # Regular data packet


# Frame types (carried in payload headers)
class FrameType(int, Enum):
    HELLO = 0x01  # Connection initiation
    HELLO_ACK = 0x02  # Connection acknowledged
    DATA = 0x10  # Payload data
    ACK = 0x11  # Selective acknowledgement
    CLOSE = 0x12  # Connection close
    RESET = 0x13  # Connection reset


# Wire format structs
_PACKET_HDR = struct.Struct(">4s4sBBHH")  # MAGIC(4) | conn_id(4) | flags(1) | seq_offset(1) | reserved(2) | payload_len(2)
_FRAME_HDR = struct.Struct(">BBHI")  # frame_type(1) | frame_flags(1) | seq_group(2) | frame_len(4)


@dataclass(frozen=True)
class PacketHeader:
    """STORM packet-level header"""
    magic: bytes
    conn_id: bytes  # 4 bytes
    flags: int
    seq_offset: int  # 0-255 (seq % 256)
    reserved: int
    payload_len: int

    @classmethod
    def unpack(cls, raw: bytes) -> PacketHeader:
        if len(raw) < _PACKET_HDR.size:
            raise ValueError("packet header too short")
        magic, conn_id, flags, seq_offset, reserved, payload_len = _PACKET_HDR.unpack(
            raw[:_PACKET_HDR.size]
        )
        if magic != MAGIC:
            raise ValueError(f"bad packet magic: {magic.hex()}")
        return cls(
            magic=magic,
            conn_id=conn_id,
            flags=flags,
            seq_offset=seq_offset,
            reserved=reserved,
            payload_len=payload_len,
        )

    def pack(self) -> bytes:
        return _PACKET_HDR.pack(
            self.magic,
            self.conn_id,
            self.flags & 0xFF,
            self.seq_offset & 0xFF,
            self.reserved & 0xFFFF,
            self.payload_len & 0xFFFF,
        )


@dataclass(frozen=True)
class FrameHeader:
    """STORM frame-level header (inside packet payload)"""
    frame_type: int
    frame_flags: int
    seq_group: int  # Which 256-packet group this frame belongs to
    frame_len: int

    @classmethod
    def unpack(cls, raw: bytes) -> FrameHeader:
        if len(raw) < _FRAME_HDR.size:
            raise ValueError("frame header too short")
        frame_type, frame_flags, seq_group, frame_len = _FRAME_HDR.unpack(
            raw[:_FRAME_HDR.size]
        )
        return cls(
            frame_type=frame_type,
            frame_flags=frame_flags,
            seq_group=seq_group,
            frame_len=frame_len,
        )

    def pack(self) -> bytes:
        return _FRAME_HDR.pack(
            self.frame_type & 0xFF,
            self.frame_flags & 0xFF,
            self.seq_group & 0xFFFF,
            self.frame_len & 0xFFFFFFFF,
        )


def make_packet(
    conn_id: bytes,
    flags: int,
    seq_offset: int,
    payload: bytes,
) -> bytes:
    """Create a complete STORM packet"""
    if len(conn_id) != 4:
        raise ValueError("conn_id must be 4 bytes")
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    
    header = PacketHeader(
        magic=MAGIC,
        conn_id=conn_id,
        flags=flags,
        seq_offset=seq_offset & 0xFF,
        reserved=0,
        payload_len=len(payload),
    )
    return header.pack() + payload


def parse_packet(raw: bytes) -> tuple[PacketHeader, bytes]:
    """Parse a STORM packet into header and payload"""
    header = PacketHeader.unpack(raw)
    start = _PACKET_HDR.size
    end = start + header.payload_len
    if len(raw) < end:
        raise ValueError("packet truncated")
    payload = raw[start:end]
    return header, payload


def make_frame(
    frame_type: int,
    frame_flags: int,
    seq_group: int,
    payload: bytes,
) -> bytes:
    """Create a STORM frame (for inside packet payload)"""
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("payload too large")
    
    header = FrameHeader(
        frame_type=frame_type & 0xFF,
        frame_flags=frame_flags & 0xFF,
        seq_group=seq_group & 0xFFFF,
        frame_len=len(payload),
    )
    return header.pack() + payload


def parse_frame(raw: bytes) -> tuple[FrameHeader, bytes]:
    """Parse a STORM frame"""
    header = FrameHeader.unpack(raw)
    start = _FRAME_HDR.size
    end = start + header.frame_len
    if len(raw) < end:
        raise ValueError("frame truncated")
    payload = raw[start:end]
    return header, payload
