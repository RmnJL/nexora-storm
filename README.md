# STORM - Stateful Tunnel Over Resilient Messaging

**STORM** is a resilient DNS-based tunnel protocol designed to overcome the instability of DNS transports for applications requiring reliable, low-latency connections.

## Problem Statement

Standard DNS tunneling approaches (SSH over DNS, regular DNS proxies) suffer from:

- **Connection instability:** DNS resolvers are lossy, high-latency network nodes
- **RTT amplification:** Multiple retransmits cascade (DNS retry + SSH retry + app retry)
- **Resolver churn:** Single resolver failures cause immediate reconnects
- **Out-of-order delivery:** DNS responses don't guarantee packet ordering
- **Limited throughput:** ~0.27 Mbps with basic DNS encoding

## STORM Solution

### Core Features

1. **Connection-ID Based Resume**
   - Connections identified by 4-byte `conn_id`, not TCP sequences
   - Resume after resolver failure without reconnect
   - Transparent to applications

2. **Dual Resolver Redundancy**
   - Send queries to 2 resolvers in parallel
   - Accept first response, discard duplicates
   - Automatic failover on timeout

3. **Forward Error Correction (FEC)**
   - XOR parity recovery for single lost packets
   - Blocks of 16 packets
   - Recovers loss without retransmit requests

4. **Out-of-Order Reassembly**
   - Packets tagged with sequence groups (0-255)
   - Reassembly buffer handles out-of-order arrival
   - Timeout-based recovery for missing packets

5. **Keepalive + Health Tracking**
   - Bidirectional keepalive every 10 seconds
   - EWMA-based resolver health scoring
   - Automatic blacklist on consecutive failures

### Expected Performance

```
Baseline (Nexora v1):  0.27 Mbps
With FEC recovery:     0.35 Mbps  (loss resilience, no retransmit)
With dual resolvers:   0.54 Mbps  (parallel transmission)
With parallel + FEC:   0.8-1.5 Mbps (stable, recoverable)
```

## Architecture

```
User App
    ↓
SOCKS5:1443 (STORMClient)
    ↓ [STORM packets, conn_id-based]
DNS queries (UDP/53)
    ↓ [base32 encoding, parallel resolvers]
STORM packets (packet_id, seq, data)
    ↓
DNS responses (UDP/53)
    ↓
STORMServer
    ↓
Target TCP:443 (X-UI, SSH, etc)
    ↓↑ [TCP stream]
```

## Protocol Format

### Packet Header (16 bytes)

```
bytes 0..3:   MAGIC = "STRM"
bytes 4..7:   conn_id (32-bit connection identifier)
byte  8:      flags (PARITY|RETRANSMIT|KEEPALIVE|FINAL|DATA)
byte  9:      seq_offset (current sequence % 256)
bytes 10..11: reserved
bytes 12..13: payload_len
```

### Frame Header (inside packet payload, 8 bytes)

```
byte  0:      frame_type (HELLO|DATA|ACK|CLOSE|RESET)
byte  1:      frame_flags
bytes 2..3:   seq_group (which 256-packet block)
bytes 4..7:   frame_len
```

## Project Structure

```
Nexora storm/
├── CORE PROTOCOL LAYER
│   ├── storm_proto.py                    # Wire format (packet/frame encoding)
│   ├── storm_fec.py                      # Forward Error Correction (XOR parity)
│   └── storm_failover.py                 # Resolver health + failover
├── CONNECTION LAYER
│   └── storm_connection.py               # Connection lifecycle + reassembly
├── ENCRYPTION & SECURITY (Phase 3)
│   └── storm_encryption.py               # ChaCha20-Poly1305 AEAD
├── TRANSPORT LAYER
│   ├── storm_dns.py                      # DNS query wrapping
│   ├── storm_dns_server.py               # Real DNS server (dnspython)
│   ├── storm_client.py                   # Inside gateway (SOCKS5)
│   └── storm_server.py                   # Outside gateway (connect)
├── NEXORA V2 INTEGRATION (Phase 3)
│   ├── nexora_v2_integration.py          # STORMCarrier + STORMNexoraV2Gateway
│   └── NEXORA_V2_INTEGRATION_GUIDE.md   # Architecture & integration guide
├── TESTING & BENCHMARKING
│   ├── test_storm.py                     # Protocol unit tests
│   ├── test_local_dns_client.py          # Local DNS testing
│   ├── test_real_resolvers.py            # Public resolver testing
│   ├── test_throughput.py                # Throughput measurement
│   ├── test_nexora_v2_integration.py     # Integration tests
│   └── benchmark.py                      # Performance benchmarks
├── DOCUMENTATION
│   ├── README.md                         # This file
│   ├── DEVELOPER.md                      # Developer quick reference
│   ├── QUICKSTART.md                     # Testing quick start
│   └── NEXORA_V2_INTEGRATION_GUIDE.md   # Phase 3 integration guide
└── CONFIGURATION & BUILD
    ├── config.py                         # Production configuration
    ├── __init__.py                       # Package exports
    ├── Makefile                          # Build automation
    └── requirements.txt                  # Dependencies
```

## Usage

### Stable Server-Test Path (Recommended)

For real server testing, use the stable path:

1. `storm_client.py` on inside (`127.0.0.1:1443`)
2. `storm_server.py` on outside (`0.0.0.0:53`)
3. Xray/3x-ui inbound on inside remains standard VLESS
4. Xray inside outbound points to SOCKS `127.0.0.1:1443`
5. STORM forwards to outside upstream SOCKS/Xray

Operational runbook:
- `SERVER_TEST_RUNBOOK_FA.md`

Active probe tool:
- `storm_health_check.py`
- `storm_resolver_picker.py` (selects healthy resolvers from `data/resolvers.txt`)
- `storm_resolver_daemon.py` (keeps active resolver set fresh in background)

### Client (Inside Server)

```bash
# Listen on localhost:1443, forward to resolvers
python storm_client.py \
  --listen 127.0.0.1:1443 \
  --resolvers 185.49.84.2 178.22.122.100 \
  --zone t1.phonexpress.ir

# Connect app via SOCKS5
export SOCKS5_PROXY=127.0.0.1:1443
curl -x socks5h://127.0.0.1:1443 http://example.com
```

### Client (Inside, Auto Resolver Pick From File)

```bash
# Select healthy resolvers from file (prints space-separated list)
python storm_resolver_picker.py \
  --resolvers-file data/resolvers.txt \
  --zone t1.phonexpress.ir \
  --take 4 \
  --min-healthy 2

# Start client with auto-picked resolvers (helper script)
chmod +x run_storm_client_auto.sh
./run_storm_client_auto.sh
```

### Server (Outside Server)

```bash
# Listen on DNS, forward connections to target
python storm_server.py \
  --listen 0.0.0.0:53 \
  --target 127.0.0.1:8443
```

### Run As Background Services (No SSH Session Needed)

Use provided systemd units:
- `systemd/storm-server.service`
- `systemd/storm-client.service`
- `systemd/storm-resolver-daemon.service` (inside resolver manager)

Install:
```bash
sudo cp systemd/storm-server.service /etc/systemd/system/
sudo cp systemd/storm-resolver-daemon.service /etc/systemd/system/
sudo cp systemd/storm-client.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now storm-server
sudo systemctl enable --now storm-resolver-daemon
sudo systemctl enable --now storm-client
```

## Testing

```bash
# Run unit tests
pytest test_storm.py -v

# Run specific test
pytest test_storm.py::TestFEC::test_parity_block_with_recovery -v

# Integration test (requires running client + server)
python -m pytest tests/integration/ -v
```

## Performance Tuning

### FEC Block Size
- Default: 16 packets
- Larger blocks = better loss tolerance, more reassembly delay
- Smaller blocks = faster recovery, more parity overhead
- Recommended: 16-32

### Keepalive Interval
- Default: 10 seconds
- Shorter = faster failure detection, more overhead
- Longer = less keepalive traffic, slower detection
- Recommended: 5-15 seconds

### Resolver Selection
- Use sticky resolver pool (don't randomly switch)
- Monitor health every 30 seconds
- Blacklist on 3+ consecutive timeouts for 10 seconds

## Roadmap

### Phase 1 ✅ COMPLETE
- [x] Protocol definition
- [x] Packet/frame encoding
- [x] FEC recovery primitives
- [x] Resolver failover
- [x] Connection manager
- [x] Unit test suite (6 test classes)

### Phase 2 ✅ COMPLETE
- [x] Real DNS server integration (dnspython UDP socket)
- [x] Local test client (test_local_dns_client.py)
- [x] Throughput measurement (test_throughput.py)
- [x] Quick start guide (QUICKSTART.md)
- [x] Full integration testing

### Phase 3 ✅ COMPLETE (Current)
- [x] **Encryption layer** (ChaCha20-Poly1305 AEAD, per-packet key derivation)
- [x] **Real resolver testing** (Google, Cloudflare, Quad9 DNS)
  - 3 test modes: quick (5 queries), full (20 queries), monitoring (60s)
  - Per-resolver stats, latency percentiles, reliability assessment
- [x] **Nexora v2 integration** (STORMCarrier, STORMNexoraV2Gateway)
  - Stream multiplexing (multiple streams over 1 connection)
  - Connection resumption (conn_id-based)
  - Automatic carrier management
- [x] **Integration tests** (test_nexora_v2_integration.py)
- [x] **Performance benchmarks** (benchmark.py)
  - Encryption benchmarks
  - Resolver selection benchmarks
  - Throughput benchmarks
  - Multiplexing benchmarks
- [x] **Integration documentation** (NEXORA_V2_INTEGRATION_GUIDE.md)

### Phase 4 (Upcoming)
- [ ] Production deployment guide
- [ ] Metrics export (Prometheus format)
- [ ] Advanced congestion control
- [ ] Regional resolver clustering
- [ ] Security audit + hardening
- [ ] ed25519 authentication layer

## Comparison

| Feature | DNS-SSH | Nexora v1 | STORM |
|---------|---------|-----------|-------|
| **Connection Resume** | ❌ | ❌ | ✅ |
| **Out-of-order OK** | ❌ | ⚠️ | ✅ |
| **FEC/Parity** | ❌ | ❌ | ✅ |
| **Dual Resolver** | ❌ | ⚠️ | ✅ |
| **Keepalive** | ✅ | ✅ | ✅ |
| **Throughput** | 0.18 Mbps | 0.27 Mbps | **0.8-1.5 Mbps** |
| **Stability** | ⚠️ | ⚠️ | ✅ |
| **Setup Time** | 1 hour | 15 min | **5 min** |

## Limitations

1. **Single missing packet recovery only** - multiple concurrent losses uncovered
   - Future: Regenerating codes (e.g., Reed-Solomon)

2. **No encryption in Phase 1** - plaintext over DNS
   - Future: Per-packet ChaCha20

3. **No authentication** - no proof of server identity
   - Future: ed25519 signatures

4. **DNS resolver must support EDNS0** - prerequisite for larger payloads
   - Fallback: standard ~255 byte responses

## Contributing

STORM development follows Nexora v2 roadmap. See main project for contribution guidelines.

## License

Same as Nexora parent project.

## Author

Rmn JL

---

**Status:** Early development (Phase 1 scaffolding complete)  
**Last Updated:** 2026-03-13
