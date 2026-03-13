"""
STORM Tests - Unit and integration tests

Run with: python -m pytest tests/
"""

import pytest
import asyncio
import secrets
from storm_proto import (
    MAGIC,
    PacketFlags,
    FrameType,
    make_packet,
    parse_packet,
    make_frame,
    parse_frame,
)
from storm_fec import FECRecovery, ParityBlock, compute_parity
from storm_failover import ResolverSelector
from storm_connection import STORMConnection, ConnectionConfig, ConnectionState


class TestProtocol:
    """Test protocol primitives"""
    
    def test_packet_roundtrip(self):
        """Test packet encoding/decoding"""
        conn_id = secrets.token_bytes(4)
        flags = PacketFlags.DATA
        seq_offset = 42
        payload = b"Hello, STORM!"
        
        packet = make_packet(conn_id, flags, seq_offset, payload)
        
        header, decoded_payload = parse_packet(packet)
        
        assert header.conn_id == conn_id
        assert header.flags == flags
        assert header.seq_offset == seq_offset
        assert decoded_payload == payload
    
    def test_frame_roundtrip(self):
        """Test frame encoding/decoding"""
        frame_type = FrameType.DATA
        frame_flags = 0
        seq_group = 1
        payload = b"Frame data"
        
        frame = make_frame(frame_type, frame_flags, seq_group, payload)
        header, decoded_payload = parse_frame(frame)
        
        assert header.frame_type == frame_type
        assert header.frame_flags == frame_flags
        assert header.seq_group == seq_group
        assert decoded_payload == payload
    
    def test_magic_validation(self):
        """Test that bad magic is rejected"""
        bad_packet = b"XXXX" + b"data"
        
        with pytest.raises(ValueError):
            parse_packet(bad_packet)


class TestFEC:
    """Test Forward Error Correction"""
    
    def test_parity_block_complete(self):
        """Test block with all packets"""
        block = ParityBlock(block_id=0, block_size=4)
        
        block.add_packet(0, b"data0")
        block.add_packet(1, b"data1")
        block.add_packet(2, b"data2")
        block.add_packet(3, b"data3")
        
        assert block.is_complete()
        assert block.missing_offset() is None
    
    def test_parity_block_with_recovery(self):
        """Test FEC recovery with parity"""
        packets = [b"data0", b"data1", b"data2", b"data3"]
        parity = compute_parity(packets)
        
        block = ParityBlock(block_id=0, block_size=4)
        block.add_packet(0, packets[0])
        block.add_packet(1, packets[1])
        block.add_packet(3, packets[3])  # Missing packet 2
        block.set_parity(parity)
        
        assert not block.is_complete()
        assert block.can_recover()
        
        recovered = block.recover_missing()
        assert recovered == packets[2]
        assert block.is_complete()
    
    def test_fec_recovery_manager(self):
        """Test FECRecovery manager"""
        fec = FECRecovery(block_size=4)
        
        packets = [b"packet0", b"packet1", b"packet2", b"packet3"]
        parity = compute_parity(packets)
        
        # Add packets except one
        fec.add_packet(0, packets[0])
        fec.add_packet(1, packets[1])
        fec.add_packet(3, packets[3])
        
        # Add parity - should recover packet 2
        recovered = fec.add_parity(0, parity)
        assert recovered == packets[2]


class TestFailover:
    """Test resolver failover"""
    
    def test_resolver_selection(self):
        """Test resolver selection"""
        resolvers = ["resolver1", "resolver2", "resolver3"]
        selector = ResolverSelector(resolvers)
        
        primary = selector.select_primary()
        assert primary in resolvers
        
        primary, secondary = selector.select_pair()
        assert primary != secondary
        assert primary in resolvers
        assert secondary in resolvers
    
    def test_resolver_health_tracking(self):
        """Test health tracking"""
        resolvers = ["good", "bad"]
        selector = ResolverSelector(resolvers)
        
        # Report successes for 'good'
        for _ in range(5):
            selector.report_success("good", 100.0)
        
        # Report failures for 'bad'
        for _ in range(3):
            selector.report_failure("bad", is_timeout=True)
        
        # 'good' should be selected
        primary = selector.select_primary()
        assert primary == "good"
    
    def test_resolver_blacklist(self):
        """Test resolver blacklisting"""
        resolvers = ["resolver1"]
        selector = ResolverSelector(resolvers, blacklist_cooldown=0.1)
        
        # Report enough failures to blacklist
        for _ in range(3):
            selector.report_failure("resolver1", is_timeout=True)
        
        health = selector.get_health("resolver1")
        assert health["blacklisted"]


class TestConnection:
    """Test connection management"""
    
    def test_connection_creation(self):
        """Test connection creation"""
        conn = STORMConnection()
        
        assert conn.state.value == "init"
        assert len(conn.conn_id) == 4
        assert conn.uptime() > 0
    
    @pytest.mark.asyncio
    async def test_send_data(self):
        """Test sending data"""
        conn = STORMConnection()
        conn.state = ConnectionState.OPEN
        
        # Queue data
        await conn.send_data(b"test data")
        
        # Get packet
        packet = conn.get_next_outgoing()
        assert packet is not None

    @pytest.mark.asyncio
    async def test_close_frame_marks_connection_closed(self):
        """CLOSE frame should transition connection state to CLOSED."""
        conn = STORMConnection()
        conn.state = ConnectionState.OPEN

        close_frame = make_frame(
            frame_type=FrameType.CLOSE,
            frame_flags=0,
            seq_group=0,
            payload=b"",
        )
        packet = make_packet(
            conn_id=conn.conn_id,
            flags=PacketFlags.DATA | PacketFlags.FINAL,
            seq_offset=0,
            payload=close_frame,
        )
        await conn.handle_incoming_packet(packet)
        assert conn.state == ConnectionState.CLOSED


class TestIntegration:
    """Integration tests"""
    
    def test_protocol_full_cycle(self):
        """Test full protocol encode/decode cycle"""
        # Create a connection ID
        conn_id = secrets.token_bytes(4)
        
        # Create a frame
        frame_data = make_frame(
            frame_type=FrameType.DATA,
            frame_flags=0,
            seq_group=0,
            payload=b"integration test",
        )
        
        # Wrap in packet
        packet = make_packet(
            conn_id=conn_id,
            flags=PacketFlags.DATA,
            seq_offset=0,
            payload=frame_data,
        )
        
        # Parse back
        header, payload = parse_packet(packet)
        frame_header, frame_payload = parse_frame(payload)
        
        assert header.conn_id == conn_id
        assert frame_header.frame_type == FrameType.DATA
        assert frame_payload == b"integration test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
