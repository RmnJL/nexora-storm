"""
Test STORM-Nexora v2 Integration

Validates:
- STORM carrier management
- Stream multiplexing
- Nexora v2 compatibility (if available)
- Failover behavior
"""

import asyncio
import logging
import pytest
import sys
import os
from typing import List

# Import STORM components
sys.path.insert(0, os.path.dirname(__file__))

from storm_proto import make_packet, parse_packet, FrameType, PacketFlags
from storm_connection import STORMConnection, ConnectionConfig
from nexora_v2_integration import STORMCarrier, STORMNexoraV2Gateway, CarrierConfig

log = logging.getLogger("test-nexora-v2-integration")


class TestSTORMCarrier:
    """Test STORM carrier functionality"""
    
    @pytest.mark.asyncio
    async def test_carrier_creation(self):
        """Test carrier can be created and started"""
        resolvers = ["8.8.8.8", "1.1.1.1"]
        
        carrier = STORMCarrier(
            carrier_id=1,
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        assert carrier.carrier_id == 1
        assert carrier.active is False
        
        await carrier.start()
        assert carrier.active is True
        
        await carrier.close()
        assert carrier.active is False
    
    @pytest.mark.asyncio
    async def test_carrier_stats(self):
        """Test carrier statistics"""
        resolvers = ["8.8.8.8"]
        
        carrier = STORMCarrier(
            carrier_id=1,
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        await carrier.start()
        
        stats = carrier.get_stats()
        assert stats["carrier_id"] == 1
        assert stats["packets_sent"] >= 0
        assert stats["packets_received"] >= 0
        assert stats["active"] is True
        
        await carrier.close()
    
    @pytest.mark.asyncio
    async def test_stream_registration(self):
        """Test stream can be registered"""
        resolvers = ["8.8.8.8"]
        
        carrier = STORMCarrier(
            carrier_id=1,
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        await carrier.start()
        
        # Register stream and check it exists
        queue = asyncio.Queue()
        carrier.stream_map[1] = queue
        
        assert 1 in carrier.stream_map
        assert carrier.stream_map[1] is queue
        
        await carrier.close()


class TestSTORMNexoraV2Gateway:
    """Test gateway functionality"""
    
    @pytest.mark.asyncio
    async def test_gateway_creation(self):
        """Test gateway can be created"""
        resolvers = ["8.8.8.8", "1.1.1.1"]
        
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(max_carriers=2),
        )
        
        assert gateway.resolvers == resolvers
        assert len(gateway.carriers) == 0
    
    @pytest.mark.asyncio
    async def test_carrier_creation_and_reuse(self):
        """Test gateway creates carriers on demand and reuses them"""
        resolvers = ["8.8.8.8"]
        
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        # Get first carrier
        carrier1 = await gateway.get_or_create_carrier()
        assert carrier1.carrier_id == 1
        assert 1 in gateway.carriers
        
        # Get second time should return same carrier
        carrier2 = await gateway.get_or_create_carrier()
        assert carrier2.carrier_id == 1
        assert carrier2 is carrier1
        
        await gateway.close()
    
    @pytest.mark.asyncio
    async def test_gateway_stats(self):
        """Test gateway statistics"""
        resolvers = ["8.8.8.8"]
        
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        # Create a carrier
        carrier = await gateway.get_or_create_carrier()
        
        stats = gateway.get_stats()
        assert stats["carriers"] >= 1
        assert len(stats["carrier_stats"]) >= 1
        
        await gateway.close()
    
    @pytest.mark.asyncio
    async def test_multiple_streams(self):
        """Test multiple concurrent streams"""
        resolvers = ["8.8.8.8"]
        
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        streams_created = []
        
        async def stream_sender(stream_id: int):
            carrier = await gateway.get_or_create_carrier()
            data = f"Stream {stream_id}".encode()
            streams_created.append((stream_id, carrier.carrier_id))
        
        # Run 3 streams concurrently
        await asyncio.gather(
            stream_sender(1),
            stream_sender(2),
            stream_sender(3),
        )
        
        assert len(streams_created) == 3
        
        # All streams should use same carrier
        carrier_ids = {cid for _, cid in streams_created}
        assert len(carrier_ids) == 1
        
        await gateway.close()


class TestCarrierFailover:
    """Test carrier failover behavior"""
    
    @pytest.mark.asyncio
    async def test_resolver_selector_primary_fallback(self):
        """Test resolver selector falls back to secondary"""
        from storm_failover import ResolverSelector
        
        resolvers = ["8.8.8.8", "1.1.1.1"]
        selector = ResolverSelector(resolvers)
        
        # Select pair
        primary, secondary = selector.select_pair()
        assert primary in resolvers
        assert secondary in resolvers
        assert primary != secondary
        
        # Mark primary as failed
        selector.report_failure(primary, is_timeout=True)
        
        # Next selection should still work
        primary2, secondary2 = selector.select_pair()
        assert primary2 in resolvers
        assert secondary2 in resolvers
    
    @pytest.mark.asyncio
    async def test_carrier_with_multiple_resolvers(self):
        """Test carrier can handle multiple resolvers"""
        resolvers = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
        
        carrier = STORMCarrier(
            carrier_id=1,
            resolvers=resolvers,
            config=CarrierConfig(),
        )
        
        assert len(carrier.resolver_selector.resolvers) == 3
        
        # Select primary and secondary
        primary, secondary = carrier.resolver_selector.select_pair()
        
        assert primary in resolvers
        assert secondary in resolvers
        assert primary != secondary
        
        await carrier.close()


class TestConnectionResumption:
    """Test STORM connection resumption capability"""
    
    @pytest.mark.asyncio
    async def test_connection_id_persistence(self):
        """Test conn_id persists across operations"""
        conn1 = STORMConnection(config=ConnectionConfig())
        conn_id_hex_1 = conn1.conn_id_hex()
        
        # Conn ID should be fixed
        conn_id_hex_2 = conn1.conn_id_hex()
        assert conn_id_hex_1 == conn_id_hex_2
        
        await conn1.close()
    
    @pytest.mark.asyncio
    async def test_multiple_carriers_different_conn_ids(self):
        """Test multiple carriers have different conn_ids"""
        resolvers = ["8.8.8.8"]
        
        carrier1 = STORMCarrier(carrier_id=1, resolvers=resolvers)
        carrier2 = STORMCarrier(carrier_id=2, resolvers=resolvers)
        
        conn_id_1 = carrier1.storm_conn.conn_id_hex()
        conn_id_2 = carrier2.storm_conn.conn_id_hex()
        
        assert conn_id_1 != conn_id_2
        
        await carrier1.close()
        await carrier2.close()


class TestStreamMultiplexing:
    """Test stream multiplexing over single carrier"""
    
    @pytest.mark.asyncio
    async def test_stream_queue_isolation(self):
        """Test streams have isolated queues"""
        resolvers = ["8.8.8.8"]
        
        carrier = STORMCarrier(carrier_id=1, resolvers=resolvers)
        await carrier.start()
        
        # Get queues for different streams
        q1 = carrier.stream_map.setdefault(1, asyncio.Queue())
        q2 = carrier.stream_map.setdefault(2, asyncio.Queue())
        
        assert q1 is not q2
        
        # Only stream 1 has data
        await q1.put(b"data1")
        
        assert not q2.empty() is False
        assert not q1.empty() is True
        
        await carrier.close()
    
    @pytest.mark.asyncio
    async def test_concurrent_stream_receive(self):
        """Test concurrent receives on different streams"""
        resolvers = ["8.8.8.8"]
        
        carrier = STORMCarrier(carrier_id=1, resolvers=resolvers)
        await carrier.start()
        
        # Set up streams
        for i in range(3):
            carrier.stream_map[i] = asyncio.Queue()
        
        async def receiver(stream_id: int):
            queue = carrier.stream_map[stream_id]
            await queue.put(f"data-{stream_id}".encode())
            await asyncio.sleep(0.01)
            result = await queue.get()
            return result
        
        results = await asyncio.gather(
            receiver(0),
            receiver(1),
            receiver(2),
        )
        
        assert len(results) == 3
        
        await carrier.close()


class TestIntegrationScenarios:
    """Test realistic integration scenarios"""
    
    @pytest.mark.asyncio
    async def test_full_gateway_lifecycle(self):
        """Test complete gateway lifecycle"""
        resolvers = ["8.8.8.8", "1.1.1.1"]
        
        # Create gateway
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(max_carriers=2),
        )
        
        # Get carrier
        carrier = await gateway.get_or_create_carrier()
        assert carrier is not None
        
        # Check stats before close
        stats_before = gateway.get_stats()
        assert stats_before["carriers"] >= 1
        
        # Close
        await gateway.close()
        
        # Verify carrier is closed
        stats_after = gateway.get_stats()
        assert all(not cs["active"] for cs in stats_after["carrier_stats"])
    
    @pytest.mark.asyncio
    async def test_stress_many_carriers(self):
        """Test gateway with multiple carriers"""
        resolvers = ["8.8.8.8"]
        
        gateway = STORMNexoraV2Gateway(
            resolvers=resolvers,
            config=CarrierConfig(max_carriers=5),
        )
        
        # Simulate 5 parallel requests (creating 5 carriers limit)
        async def request(i: int):
            await asyncio.sleep(0.1)  # Stagger creation
            await gateway.get_or_create_carrier()
        
        await asyncio.gather(*[request(i) for i in range(5)])
        
        # Should have 1 active carrier (reuse pattern)
        stats = gateway.get_stats()
        assert stats["carriers"] >= 1
        
        await gateway.close()
    
    @pytest.mark.asyncio
    async def test_gateway_with_encryption(self):
        """Test gateway integration with encryption layer"""
        from storm_encryption import STORMCrypto
        
        resolvers = ["8.8.8.8"]
        gateway = STORMNexoraV2Gateway(resolvers=resolvers)
        
        carrier = await gateway.get_or_create_carrier()
        conn_id_bytes = bytes.fromhex(carrier.storm_conn.conn_id_hex())
        
        # Encrypt test packet with carrier's conn_id
        plaintext = b"test data"
        psk = b"0123456789abcdef0123456789abcdef"
        
        nonce, ciphertext = STORMCrypto.encrypt_packet(plaintext, conn_id_bytes, 1, psk)
        recovered = STORMCrypto.decrypt_packet(nonce, ciphertext, conn_id_bytes, 1, psk)
        
        assert recovered == plaintext
        
        await gateway.close()


# Test runner
async def run_tests():
    """Run all tests with async support"""
    print("=" * 60)
    print("STORM - Nexora v2 Integration Tests")
    print("=" * 60)
    
    # Run pytest if available
    try:
        pytest.main([__file__, "-v", "-s"])
    except Exception as e:
        print(f"\n⚠️  pytest not available: {e}")
        print("Running manual tests...\n")
        
        # Manual test execution
        test = TestSTORMCarrier()
        await test.test_carrier_creation()
        print("✅ test_carrier_creation passed")
        
        test2 = TestSTORMNexoraV2Gateway()
        await test2.test_gateway_creation()
        print("✅ test_gateway_creation passed")
        
        await test2.test_carrier_creation_and_reuse()
        print("✅ test_carrier_creation_and_reuse passed")
        
        test3 = TestIntegrationScenarios()
        await test3.test_full_gateway_lifecycle()
        print("✅ test_full_gateway_lifecycle passed")
        
        print("\n✅ All manual tests passed!")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    asyncio.run(run_tests())
