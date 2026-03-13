"""
STORM FEC Module - Forward Error Correction via parity

Implements XOR-based single-missing-packet recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParityBlock:
    """One FEC block containing up to N packets"""
    block_id: int
    block_size: int
    packets: dict[int, bytes] = field(default_factory=dict)
    parity_data: Optional[bytes] = None
    
    def add_packet(self, offset: int, data: bytes) -> None:
        """Add packet at offset within this block"""
        if offset < 0 or offset >= self.block_size:
            raise ValueError(f"offset {offset} out of range [0, {self.block_size})")
        self.packets[offset] = data
    
    def set_parity(self, data: bytes) -> None:
        """Set the parity packet"""
        self.parity_data = data
    
    def missing_offset(self) -> Optional[int]:
        """Find single missing packet offset, or None if complete"""
        for i in range(self.block_size):
            if i not in self.packets:
                return i
        return None
    
    def is_complete(self) -> bool:
        """Check if all packets received (with or without recovery)"""
        return len(self.packets) == self.block_size
    
    def can_recover(self) -> bool:
        """Check if we can recover: have N-1 packets + parity for N packets"""
        missing = self.missing_offset()
        if missing is None:
            return True  # Already complete
        if self.parity_data is None:
            return False  # No parity available
        # Count non-missing packets
        non_missing = len([p for p in self.packets.keys() if p != missing])
        return non_missing == self.block_size - 1
    
    def recover_missing(self) -> Optional[bytes]:
        """
        Recover missing packet using XOR:
        missing_packet = parity XOR packet[0] XOR packet[1] XOR ... XOR packet[N-1]
        """
        missing = self.missing_offset()
        if missing is None:
            return None  # Nothing missing
        if self.parity_data is None:
            return None  # No recovery possible
        
        # XOR all packets including parity
        recovered = bytearray(self.parity_data)
        for offset in range(self.block_size):
            if offset in self.packets:
                pkt = self.packets[offset]
                # Ensure same length
                if len(recovered) < len(pkt):
                    recovered.extend(b'\x00' * (len(pkt) - len(recovered)))
                elif len(pkt) < len(recovered):
                    pkt = pkt + b'\x00' * (len(recovered) - len(pkt))
                # XOR bytes
                for i in range(len(recovered)):
                    recovered[i] ^= pkt[i]
        
        self.packets[missing] = bytes(recovered)
        return bytes(recovered)
    
    def get_ordered_packets(self) -> list[bytes]:
        """Return packets in order, or raise if incomplete"""
        if not self.is_complete():
            missing = self.missing_offset()
            raise ValueError(f"block {self.block_id} incomplete: missing offset {missing}")
        return [self.packets[i] for i in range(self.block_size)]


class FECRecovery:
    """Manages FEC recovery across multiple blocks"""
    
    def __init__(self, block_size: int = 16):
        self.block_size = max(1, min(block_size, 256))
        self.blocks: dict[int, ParityBlock] = {}
    
    def add_packet(self, seq: int, data: bytes) -> Optional[bytes]:
        """
        Add a data packet. Returns recovered packet if recovery completed, else None.
        seq is the absolute sequence number (0, 1, 2, ...)
        """
        block_id = seq // self.block_size
        offset = seq % self.block_size
        
        if block_id not in self.blocks:
            self.blocks[block_id] = ParityBlock(block_id, self.block_size)
        
        block = self.blocks[block_id]
        block.add_packet(offset, data)
        
        # Try recovery if we now have enough
        if block.can_recover() and not block.is_complete():
            return block.recover_missing()
        
        return None
    
    def add_parity(self, seq_base: int, parity_data: bytes) -> Optional[bytes]:
        """
        Add parity data for a block starting at seq_base.
        seq_base must be aligned to block boundary.
        Returns recovered packet if recovery completed, else None.
        """
        if seq_base % self.block_size != 0:
            raise ValueError(f"seq_base {seq_base} not block-aligned")
        
        block_id = seq_base // self.block_size
        
        if block_id not in self.blocks:
            self.blocks[block_id] = ParityBlock(block_id, self.block_size)
        
        block = self.blocks[block_id]
        block.set_parity(parity_data)
        
        # Try recovery
        if block.can_recover() and not block.is_complete():
            return block.recover_missing()
        
        return None
    
    def get_block(self, block_id: int) -> Optional[ParityBlock]:
        """Get a block by ID"""
        return self.blocks.get(block_id)
    
    def cleanup_old_blocks(self, keep_last_n: int = 10) -> None:
        """Clean up old blocks to save memory"""
        if len(self.blocks) > keep_last_n:
            max_id = max(self.blocks.keys())
            min_keep = max_id - keep_last_n
            to_delete = [bid for bid in self.blocks.keys() if bid < min_keep]
            for bid in to_delete:
                del self.blocks[bid]


def compute_parity(packets: list[bytes]) -> bytes:
    """
    Compute parity (XOR) of a list of packets.
    All packets should be same length.
    """
    if not packets:
        return b''
    
    result = bytearray(len(packets[0]))
    for pkt in packets:
        pkt_padded = pkt + b'\x00' * (len(result) - len(pkt))
        for i in range(len(result)):
            result[i] ^= pkt_padded[i]
    
    return bytes(result)
