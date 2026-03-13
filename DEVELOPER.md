# STORM Developer Quick Reference

## Project Structure

```
storm_proto.py
  ├─ MAGIC, VERSION, PacketFlags, FrameType enums
  ├─ PacketHeader, FrameHeader dataclasses
  └─ make_packet/parse_packet, make_frame/parse_frame functions

storm_fec.py
  ├─ ParityBlock: single FEC block management
  ├─ FECRecovery: multi-block FEC controller
  └─ compute_parity: XOR-based parity calculation

storm_failover.py
  ├─ ResolverHealth: per-resolver metrics
  └─ ResolverSelector: active resolver selection + failover

storm_connection.py
  ├─ ConnectionState: state machine
  ├─ ConnectionConfig: configuration
  ├─ STORMConnection: single connection lifecycle
  └─ ConnectionManager: connection pool

storm_dns.py
  ├─ DNSTransport: packet <-> DNS query encoding
  └─ DNSGateway: parallel resolver queries

storm_client.py
  └─ STORMClient: inside gateway (local proxy)

storm_server.py
  ├─ TargetConnector: TCP connection management
  └─ STORMServer: outside gateway (DNS listener)
```

## Data Flow

### Client Side (send)
```
User App (TCP:1443)
  ↓ (asyncio.StreamReader)
STORMClient.handle_connection()
  ↓
STORMConnection.send_data()
  ↓ (queue chunks)
STORMConnection.get_next_outgoing()
  ↓ (make_packet + make_frame)
byte[] packet
  ↓
ResolverSelector.select_pair()
  ↓
DNSGateway.send_to_any()
  ↓ (encode_packet -> base32)
DNS Query (UDP/53)
  ↓
Public Resolvers
```

### Server Side (receive)
```
DNS Query (UDP/53)
  ↓
STORMServer.handle_dns_query()
  ↓ (decode base32)
byte[] packet
  ↓
STORMServer._process_storm_packet()
  ↓ (parse_packet -> parse_frame)
STORMConnection.handle_incoming_packet()
  ↓
FECRecovery or reassembly buffer
  ↓
TargetConnector (TCP:8443)
  ↓
Target Service (X-UI, SSH, etc)
```

## Key Classes API

### STORMConnection

```python
# Create
conn = STORMConnection()

# State
conn.state           # ConnectionState enum
conn.uptime()        # float seconds

# Send data
await conn.send_data(data)          # Queue for transmission
packet = conn.get_next_outgoing()   # Get packet for DNS

# Receive data
await conn.handle_incoming_packet(packet)
data = await conn.get_ordered_data()  # Get reassembled data

# Lifecycle
await conn.close()
```

### FECRecovery

```python
fec = FECRecovery(block_size=16)

# Add packets
recovered = fec.add_packet(seq=0, data=b"...")
recovered = fec.add_parity(seq_base=0, parity_data=b"...")

# Check block status
block = fec.get_block(block_id=0)
block.is_complete()
block.can_recover()
block.get_ordered_packets()
```

### ResolverSelector

```python
selector = ResolverSelector(["8.8.8.8", "1.1.1.1"])

# Select resolver
primary = selector.select_primary()
primary, secondary = selector.select_pair()

# Report results
selector.report_success(resolver, latency_ms=100)
selector.report_failure(resolver, is_timeout=True)

# Health snapshot
health = selector.get_health()  # All resolvers
health = selector.get_health("8.8.8.8")  # Specific
```

## Testing

### Run all tests
```bash
pytest test_storm.py -v
```

### Run specific test class
```bash
pytest test_storm.py::TestFEC -v
pytest test_storm.py::TestFailover -v
```

### Run with coverage
```bash
pytest test_storm.py --cov=. --cov-report=html
```

### Run example
```bash
python example_local_test.py
```

## Common Tasks

### Add new packet type
1. Add to `PacketFlags` enum in `storm_proto.py`
2. Handle in `STORMConnection.handle_incoming_packet()`
3. Add test in `test_storm.py`

### Add new frame type
1. Add to `FrameType` enum in `storm_proto.py`
2. Handle in `STORMConnection._handle_data_frame()`
3. Add test

### Add resolver
```python
resolvers.append("8.8.8.8")
selector = ResolverSelector(resolvers)
```

### Tune FEC
```python
# Larger block = better loss tolerance
# Smaller block = faster recovery
config = ConnectionConfig(block_size=32)  # Instead of 16
```

### Change keepalive
```python
config = ConnectionConfig(keepalive_interval=5.0)  # 5 seconds
```

## Performance Considerations

- **Packet size**: 200 bytes default (tuned for DNS limits)
- **FEC overhead**: ~25% (1 parity per 4 packets)
- **Reassembly timeout**: 30 seconds (grace period for slow packets)
- **Keepalive**: 10 seconds (balance between detection speed and overhead)

## Debugging

### Enable debug logging
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Trace packet flow
```python
import logging
logger = logging.getLogger("storm")
logging.getLogger("storm").setLevel(logging.DEBUG)
```

## Integration Points

### With Nexora
- STORM replaces per-connection DNS session in Nexora v1
- Live alongside Nexora v2 as alternative transport layer
- Integrate `STORMConnection` into Nexora's `StreamMux`

### With X-UI
- Server targets X-UI VLESS port (default 8443)
- No code changes to X-UI needed (transparent proxy)

### With SSH
- Tunnel SSH traffic through SOCKS5:1443
- SSH doesn't notice lossy/latent transport

## Links

- Nexora Main: `../nexora/`
- Protocol Spec: `docs/V2_PROTOCOL_SPEC.md`
- Rollout Plan: `docs/V2_ROLLOUT_PLAN.md`

---

**Last Updated:** 2026-03-13
