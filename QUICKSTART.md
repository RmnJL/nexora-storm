# STORM Local Testing & DNS Integration - Quick Start

## Prerequisites

Install dependencies:
```bash
pip install -r requirements.txt
```

Verify installation:
```bash
python -c "import dns; print('✅ dnspython OK')"
python -c "import asyncio; print('✅ asyncio OK')"
```

---

## Phase 1: Test Protocol Locally

### Test 1: Protocol Primitives (No DNS)

```bash
cd "Nexora storm"
python example_local_test.py
# Select option: 1
```

**Expected Output:**
```
✅ STORM packet ready to send via DNS
Packet (hex): 53545...(long hex string)...
Packet size: XX bytes
Connection ID: abcd1234...
Payload: Hello from STORM protocol!
```

**What's tested:**
- ✅ Packet encoding/decoding
- ✅ Frame creation
- ✅ FEC recovery (XOR parity)
- ✅ Resolver health tracking

---

## Phase 2: DNS Integration

### Test 2A: Start DNS Server

**Terminal 1: Run DNS Server**
```bash
cd "Nexora storm"
python storm_dns_server.py
```

**Expected Output:**
```
STORM DNS Server Demo
Listening on 127.0.0.1:5353
Press Ctrl+C to stop

STORM DNS server starting on 127.0.0.1:5353
```

### Test 2B: Send DNS Queries

**Terminal 2: Run DNS Test Client**
```bash
cd "Nexora storm"
python test_local_dns_client.py
# Select option: 1
```

**Expected Output:**
```
STORM DNS Test Client
======================================================
Make sure storm_dns_server.py is running!

📦 Creating test packet...
  conn_id: a1b2c3d4
  packet size: 58 bytes

📋 DNS Query:
  name: a1b2c3.gexzqq3dm5zw64...t1.phonexpress.ir
  type: TXT
  size: 185 bytes

📤 Sending query to 127.0.0.1:5353...

✅ Received response!
  size: 142 bytes
  rcode: NOERROR
  answer records: 1

✅ DNS query/response cycle successful!
```

**Terminal 1 (Server):**
```
📨 Received STORM packet from 127.0.0.1:54321 (58 bytes)
  ✅ Parsed: conn_id=a1b2c3d4, flags=0x20, payload=47B
  📤 Sending response (58 bytes)...
```

### Test 2C: Multiple Packets

```bash
# Terminal 2:
python test_local_dns_client.py
# Select option: 2
```

**Expected Output:**
```
Testing Multiple Packets
======================================================

Test 1/3...
  ✅ Response received (58 bytes)
Test 2/3...
  ✅ Response received (58 bytes)
Test 3/3...
  ✅ Response received (58 bytes)

✅ All tests completed!
```

---

## Phase 3: Full Integration Test

### Run Complete Local Example

```bash
cd "Nexora storm"
python example_local_test.py
# Select option: 3
```

This runs DNS server internally and tests everything end-to-end.

---

## Troubleshooting

### "Address already in use" on port 5353

The port is already bound. Either:
1. Kill the previous process: `pkill -f "storm_dns_server.py"`
2. Use a different port in the code

### "dnspython not found"

```bash
pip install dnspython>=2.4.0
```

### DNS server shows "query from" but client times out

The server might not be echoing responses properly. Check:
1. Server console shows packet received
2. Client is querying the right IP:port (127.0.0.1:5353)

### "bad base32" error

The packet encoding/decoding has an issue. Make sure both client and server use same:
- MAGIC bytes ("STRM")
- Zone name (t1.phonexpress.ir)

---

## Next Steps After Successful Tests

### 1. Real Resolver Testing

Once local tests pass:

```python
# Replace 127.0.0.1:5353 with real resolver
# In storm_dns_server.py:
server = STORMDNSServer(
    listen_host="0.0.0.0",    # Listen on all interfaces
    listen_port=53,            # Standard DNS port
    tunnel_zone="t1.phonexpress.ir",  # Real zone
    connection_handler=handle_storm_packet,
)

# Run on outside server (Ubuntu inside)
# Update resolvers in test client to real resolver IPs
```

### 2. Integration with Nexora

Add to Nexora v2 as transport layer:
```python
# In nexora_v2.py:
from storm_connection import STORMConnection
from Nexora_storm.storm_client import STORMClient

# Use STORMClient instead of direct DNS tunnel
client = STORMClient(resolvers=[...])
```

### 3. Performance Benchmarking

Run throughput test:
```bash
python test_throughput.py  # (to be created)
```

Expected: **0.5-1.5 Mbps** stable

---

## File Reference

| File | Purpose |
|------|---------|
| `storm_proto.py` | Protocol primitives |
| `storm_fec.py` | FEC recovery |
| `storm_failover.py` | Resolver management |
| `storm_connection.py` | Connection lifecycle |
| `storm_dns.py` | DNS query/response wrapping |
| `storm_dns_server.py` | **Real DNS server implementation** |
| `example_local_test.py` | Example runner |
| `test_local_dns_client.py` | **DNS test client** |
| `test_storm.py` | Unit tests |

---

## Testing Timeline

```
Phase 1 (Now):
├─ Protocol test (example_local_test.py #1)
├─ DNS server (storm_dns_server.py)
└─ DNS client (test_local_dns_client.py)

Phase 2 (Next):
├─ Integration with real resolvers
├─ Performance benchmarking
└─ Stability testing under loss

Phase 3 (Future):
├─ Encryption layer
├─ Authentication
└─ Nexora v2 integration
```

---

## Example Session

```bash
# Terminal 1: Start DNS server
$ python storm_dns_server.py
STORM DNS Server Demo
Listening on 127.0.0.1:5353
Press Ctrl+C to stop

# Terminal 2: Run tests
$ python example_local_test.py
🎯 STORM Protocol - Quick Start Examples

Options:
  1. Test protocol directly (no DNS)
  2. Run local DNS server
  3. Both

Select (1/2/3) [default=1]: 3

# ... tests run ...

# Terminal 3 (optional): Manual DNS query
$ dig @127.0.0.1 -p 5353 example.t1.phonexpress.ir TXT
```

---

**Status:** Phase 1 & 2 complete, ready for Phase 3 🚀

**Last Updated:** 2026-03-13
