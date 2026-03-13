"""
STORM - Nexora v2 Integration Documentation

This document explains how STORM integrates with Nexora v2 to create
a resilient, multiplexed DNS tunnel with connection resumption.
"""

# ============================================================================
# 1. ARCHITECTURE OVERVIEW
# ============================================================================

"""
NEXORA V1 (Current - Problematic):
├── Client (5.210.94.207:5000)
│   └── TCP → per-connection DNS query → sid=None inconsistency
│       └── Single resolver
│           └── Forward timeout (40s limit)
│               └── Target (Nexora server)
└── Issue: Lost packets = full retry cycle, resolver churn on failure

STORM + NEXORA V2 (Proposed - Resilient):
├── Inside Gateway (Client @ 5.210.94.207)
│   └── TCP connection (SOCKS5 proxy)
│       └── Sends data to STORM Carrier (stream-based)
│           └── Carrier multiplexes over single STORM connection
│               └── STORM connection is resumable (conn_id)
│                   └── FEC recovery (6.25% overhead for 1 missing packet)
│                       └── Dual resolvers (failover on health score)
│                           └── Encrypted with ChaCha20-Poly1305
│                               └── Outside Gateway (Target)
│                                   └── TCP to service
└── Resilience: Lost packet recovered via FEC, not retransmit

KEY IMPROVEMENT: Connection-Centric vs Packet-Centric
  Nexora v1:  Packet → DNS → Can we get it? → Retry or fail
  STORM:      Connection → Carrier (resumable) → Packets self-heal
  
  Result: Network failures don't cascade to application layer
"""

# ============================================================================
# 2. COMPONENT LAYERS
# ============================================================================

"""
┌───────────────────────────────────────────────────────────────┐
│ NEXORA V2 Application Layer                                    │
│ (StreamMux - multiplexes multiple logical streams)              │
└────────────────────┬────────────────────────────────────────────┘
                     │ stream_id → data
┌────────────────────▼────────────────────────────────────────────┐
│ STORM.Carrier (Multiplexing)                                   │
│ - Accepts streams: recv_stream(stream_id)                      │
│ - Maintains stream_map: {stream_id → asyncio.Queue}            │
│ - Connection-based model (not packet-based)                     │
└────────────────────┬────────────────────────────────────────────┘
                     │ STORM packets
┌────────────────────▼────────────────────────────────────────────┐
│ STORM.Connection (Reliability)                                 │
│ - Packet assembly/reassembly                                   │
│ - FEC recovery (XOR parity)                                    │
│ - Selective ACK (bitmap)                                       │
│ - Resumable: conn_id (4 bytes, survives DNS query loss)       │
└────────────────────┬────────────────────────────────────────────┘
                     │ Encrypted STORM packets
┌────────────────────▼────────────────────────────────────────────┐
│ STORM.Encryption (Security)                                    │
│ - ChaCha20-Poly1305 AEAD                                       │
│ - Per-packet key derivation (HKDF)                             │
│ - Nonce: random, Auth tag: 16 bytes                            │
│ - Tampering detection                                          │
└────────────────────┬────────────────────────────────────────────┘
                     │ base32-encoded packets
┌────────────────────▼────────────────────────────────────────────┐
│ STORM.DNS (Transport)                                          │
│ - Wraps packets in DNS queries                                 │
│ - base32 encoding (DNS label safe)                             │
│ - Supports EDNS0 for large packets (up to 4096 bytes)          │
│ - Session ID: hex(conn_id)                                    │
└────────────────────┬────────────────────────────────────────────┘
                     │ DNS query over UDP/53
┌────────────────────▼────────────────────────────────────────────┐
│ ResolverSelector (Failover)                                    │
│ - Primary + secondary resolver                                 │
│ - EWMA health scoring                                          │
│ - Automatic blacklisting on repeated failures                  │
│ - Latency tracking                                             │
└────────────────────┬────────────────────────────────────────────┘
                     │ UDP/53 to resolver
                    [PUBLIC DNS RESOLVER]
                     │ (Google, Cloudflare, Quad9)
                     │
                [OUTSIDE GATEWAY - STORM DNS SERVER]
                     │
                    [TARGET SERVICE]
"""

# ============================================================================
# 3. API USAGE PATTERNS
# ============================================================================

"""
PATTERN A: Simple carrier usage
─────────────────────────────────

    from nexora_v2_integration import STORMCarrier
    
    carrier = STORMCarrier(
        carrier_id=1,
        resolvers=["8.8.8.8", "1.1.1.1"],
    )
    await carrier.start()
    
    # Send data on a stream
    await carrier.multiplex_stream(stream_id=1, data=b"hello")
    
    # Receive data on a stream
    response = await carrier.receive_stream(stream_id=1, timeout=30)
    
    await carrier.close()


PATTERN B: Gateway-based usage
───────────────────────────────

    from nexora_v2_integration import STORMNexoraV2Gateway
    
    gateway = STORMNexoraV2Gateway(
        resolvers=["8.8.8.8", "1.1.1.1", "9.9.9.9"],
        zone="t1.phonexpress.ir",
    )
    
    # Send data (auto-creates or reuses carrier)
    await gateway.send_data(stream_id=1, data=b"data")
    
    # Receive data
    response = await gateway.receive_data(stream_id=1)
    
    # Get statistics
    stats = gateway.get_stats()
    # {
    #   "carriers": 1,
    #   "carrier_stats": [
    #     {
    #       "carrier_id": 1,
    #       "packets_sent": 42,
    #       "packets_received": 39,
    #       "active": True,
    #     }
    #   ]
    # }
    
    await gateway.close()


PATTERN C: Nexora v2 CarrierManager integration
−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−−

    # If Nexora v2 is available, STORM can replace DNS transport:
    
    from nexora.src.nexora_v2 import CarrierManager
    from nexora_v2_integration import STORMNexoraV2Gateway
    
    # Create STORM gateway as transport
    storm_gateway = STORMNexoraV2Gateway(
        resolvers=["8.8.8.8", "1.1.1.1"],
    )
    
    # Wrap in Nexora v2 CarrierManager
    carriers = CarrierManager.from_gateway(storm_gateway)
    
    # Now use standard Nexora v2 API:
    await carriers.send_on_stream(stream_id=1, data=b"payload")
    response = await carriers.recv_on_stream(stream_id=1)
"""

# ============================================================================
# 4. CONNECTION RESUMPTION MECHANISM
# ============================================================================

"""
Problem (Nexora v1):
  Connection dies if DNS query lost → TCP broken → sid=None → restart

Solution (STORM + Nexora v2):
  
  Step 0: Connection established
  ┌────────────────────────────────────────┐
  │ STORM Connection ID: 0x1a2b3c4d        │
  │ State: OPEN                            │
  │ Seq: 100 (last sent)                   │
  │ Ack: 95 (last received)                │
  └────────────────────────────────────────┘
  
  Step 1: DNS query lost mid-flight
  ┌────────────────────────────────────────┐
  │ Client sends packet via resolver       │
  │ Query: "STRM.00000100.0x1a2b3c4d.dns" │
  │ ... packet never reaches server ...    │
  └────────────────────────────────────────┘
  
  Step 2: Loss detected (RTO timeout)
  Client → FEC parity block sent
  ┌────────────────────────────────────────┐
  │ Parity packet arrives (slower path)    │
  │ Server: XOR parity with received       │
  │ Recovers → forwards to service         │
  └────────────────────────────────────────┘
  
  Step 3: If RTO expires anyway
  ┌────────────────────────────────────────┐
  │ Client sends RESET frame               │
  │ Seq: 101 (continue from 100)           │
  │ Server processes ACKs for 95+, resends │
  │ Application sees: NO INTERRUPTION      │
  └────────────────────────────────────────┘
  
  Key: Connection survives because:
    - conn_id is 4 bytes (hex "1a2b3c4d" in DNS names)
    - FEC can recover without asking for resend
    - ACK+RESET can skip lost packet entirely
"""

# ============================================================================
# 5. FAILOVER BEHAVIOR
# ============================================================================

"""
Dual-Resolver Strategy:
  
  ┌──────────────────────────────────────────┐
  │ Primary: 8.8.8.8 (Google)                │
  │ Health Score: EWMA = 0.95 (good)        │
  │ Recent: 19 success / 20 queries          │
  └──────────────────────────────────────────┘
  
  ┌──────────────────────────────────────────┐
  │ Secondary: 1.1.1.1 (Cloudflare)         │
  │ Health Score: EWMA = 0.98 (excellent)   │
  │ Recent: 20 success / 20 queries          │
  └──────────────────────────────────────────┘
  
  Packet transmission logic:
  
    packet = build_storm_packet()
    
    if primary.health > 0.9:
        send(packet, primary)
    else:
        # Primary degraded, use secondary
        send(packet, secondary)
    
    # Both sent in parallel for REDUNDANCY (fallback):
    if response from either:
        ok, conn_id improves
    if timeout on both:
        FEC parity recovery next
    if both fail 3x:
        mark primary:BLACKLISTED, use secondary only
  
  Outcome:
    - Normal: 1 packet × 1 resolver = 1 DNS query (efficient)
    - Degraded: 1 packet × 2 resolvers = 2 DNS queries (redundant)
    - Failed: FEC parity recovery (no additional query needed)
"""

# ============================================================================
# 6. METRICS & MONITORING
# ============================================================================

"""
Per-Carrier Metrics:

  carrier.get_stats() returns:
  {
    "carrier_id": 1,
    "packets_sent": 150,
    "packets_received": 145,
    "active": True,
    "storm_conn": "1a2b3c4d",  # connection ID
  }
  
  Derived metrics:
  - Success rate: packets_received / packets_sent = 145/150 = 96.67%
  - Loss rate: 1 - success_rate = 3.33%
  - FEC efficiency: If 1 parity recovered 1 packet, FEC ROI = 100% vs 25%
  - Resolver preference: Track which resolver succeeds most often
  
 
Per-Gateway Metrics:

  gateway.get_stats() returns:
  {
    "carriers": 2,
    "carrier_stats": [
      {"carrier_id": 1, ...},
      {"carrier_id": 2, ...},
    ],
    "nexora_v2_available": True,
  }
  
  Gateway-level insights:
  - Total carriers active: 2 (indicates concurrent load)
  - Total packets: sum(packets_sent) across carriers
  - Carrier lifetime: time since carrier created
  - Stream count: len(accumulate carrier.stream_map values)


Real-time monitoring:

  async def monitor_gateway(gateway):
      while True:
          stats = gateway.get_stats()
          for carrier_stats in stats['carrier_stats']:
              cid = carrier_stats['carrier_id']
              sent = carrier_stats['packets_sent']
              recv = carrier_stats['packets_received']
              rate = recv/sent if sent > 0 else 0
              print(f"Carrier {cid}: {rate*100:.1f}% success ({recv}/{sent})")
          await asyncio.sleep(10)
"""

# ============================================================================
# 7. INTEGRATION WITH NEXORA V2
# ============================================================================

"""
Nexora v2 Architecture (from docs/V2_FLOW_STATE_MACHINE.md):

  ┌─────────────────────────────────────────┐
  │ Nexora V2 API (user-facing)             │
  ├─────────────────────────────────────────┤
  │ - CarrierManager (manages pool)          │
  │ - StreamMux (multiplexes streams)        │
  │ - pack_envelope / unpack_envelope        │
  └────────┬────────────────────────────────┘
           │ uses
      ┌────▼────────────────────────────────┐
      │ Carrier (transport agnostic)        │
      ├─────────────────────────────────────┤
      │ Interface:
      │ - send(data, stream_id)
      │ - recv(stream_id) → data
      │ - close()
      └────┬────────────────────────────────┘
           │ was: DNS protocol
           │ now: STORM (our implementation)
      ┌────▼────────────────────────────────┐
      │ STORM Transport (our code)          │
      ├─────────────────────────────────────┤
      │ - STORMNexoraV2Gateway              │
      │ - STORMCarrier                      │
      │ - ResolverSelector                  │
      │ - Encryption (ChaCha20)             │
      └─────────────────────────────────────┘

Replacement strategy:

  Old:  CarrierManager → DNS (lossy, no recovery)
  New:  CarrierManager → STORM (self-healing, resilient)
  
  Steps:
  1. Import STORMNexoraV2Gateway
  2. Create gateway: storm_gw = STORMNexoraV2Gateway(resolvers=[...])
  3. Initialize carriers: carriers = CarrierManager()
  4. Replace transport: carriers.transport = storm_gw
  5. Use standard Nexora v2 API: await carriers.send_on_stream(...)


Backward compatibility:
  ✓ STORMCarrier implements Carrier interface
  ✓ send_data(stream_id, data) compatible
  ✓ receive_data(stream_id) compatible
  ✓ get_stats() for monitoring
  ✓ Works with Nexora v2 if available, falls back independently
"""

# ============================================================================
# 8. TESTING & DEPLOYMENT
# ============================================================================

"""
Testing checklist:

  Unit Tests (test_nexora_v2_integration.py):
  □ Carrier creation & startup
  □ Stream registration
  □ Gateway creation
  □ Carrier reuse
  □ Multiple concurrent streams
  □ Failover behavior
  □ Connection resumption
  □ Full lifecycle

Integration Tests:
  □ Run against localhost DNS (test_local_dns_client.py)
  □ Run against real resolvers (test_real_resolvers.py)
  □ Measure throughput (test_throughput.py)
  □ Validate encryption (storm_encryption.py tests)

Deployment steps:

  1. Production setup:
     - Resolver list: ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
     - Zone: "t1.phonexpress.ir"
     - Max carriers: 3 (conserve connections)
     - Keepalive: 10s
  
  2. Start outside gateway:
     await STORMDNSServer(zone="t1.phonexpress.ir").start()
  
  3. Start inside gateway:
     gateway = STORMNexoraV2Gateway(resolvers=[...])
     # Use with Nexora v2 or standalone
  
  4. Monitor:
     while True:
         stats = gateway.get_stats()
         log(stats)
         await asyncio.sleep(30)
  
  5. Graceful shutdown:
     await gateway.close()
"""

# ============================================================================
# 9. TROUBLESHOOTING
# ============================================================================

"""
Problem: Carrier not starting
─────────────────────────────
Symptom: carrier.active remains False, no errors
Cause:   DNS server not listening or resolvers unreachable
Fix:     
  □ Check DNS server is running on :53
  □ Verify resolvers are reachable (ping 8.8.8.8)
  □ Check firewall allows UDP/53

Problem: High packet loss
────────────────────────
Symptom: packets_received << packets_sent
Cause:   1) Resolvers flaky  2) Network loss  3) Timeout too short
Fix:
  □ Switch to different resolver
  □ Increase connection RTO: config.rto_initial = 2.0
  □ Check resolver health: ResolverSelector.get_health()

Problem: Encryption errors
──────────────────────────
Symptom: "Ciphertext authentication tag invalid"
Cause:   1) Wrong PSK  2) Packet corrupted  3) Seq mismatch
Fix:
  □ Verify PSK matches both ends
  □ Check packet integrity (isn't get corruption in TCP layer?)
  □ Validate conn_id matches

Problem: Streams stuck
─────────────────────
Symptom: receive_stream() timeout on valid stream_id
Cause:   1) Carrier not multiplexing  2) Queue never filled
Fix:
  □ Check carrier.stream_map has stream_id
  □ Verify send() is being called
  □ Check carrier logs for errors

Problem: Connection drops after 40s
─────────────────────────────────
Symptom: Data stops after ~40s (Nexora v1 behavior)
Cause:   Old forward_timeout still active elsewhere
Fix:
  □ Increase RTO max: config.rto_max = 60.0
  □ Enable keepalive: config.keepalive_interval = 10.0 (enabled by default)
  □ Check conn_id is persisting across DNS queries
"""

# ============================================================================
# 10. PERFORMANCE TARGETS
# ============================================================================

"""
Expected throughput with STORM + Nexora v2:

  Baseline (single packet, single resolver):
  - Packet size: 512 bytes (EDNS0)
  - Latency: 50ms (avg)
  - Throughput: 512 / 0.05 = 10.24 Mbps

  With FEC (6.25% overhead, recovers 1 loss):
  - Effective: 10.24 * 0.9375 = 9.6 Mbps (but loss doesn't cascade!)

  With dual resolvers (parallel sends):
  - Latency: 50ms still (first wins)
  - Reliability: 99%+ (one resolver always succeeds)

  With streams (multiplexing 3 concurrent):
  - Total: 3 * 10.24 = 30.72 Mbps aggregate
  - Per-stream: 10.24 Mbps latency-aware

  Real-world (with encryption + failures):
  - Expected: 5-8 Mbps stable
  - Peak: 10+ Mbps possible
  - Variance: ±20% depending on resolver health

Target success rate: 99%+ (FEC should handle remaining 1%)
Target latency (p95): <200ms
Target jitter (p95-p5): <100ms
Target connection resumption: <1s recovery after resolver failure
"""

# ============================================================================
# 11. REFERENCES
# ============================================================================

"""
Key files:
  - storm_proto.py          : Wire format (packet/frame)
  - storm_connection.py     : Connection lifecycle
  - storm_encryption.py     : ChaCha20-Poly1305 AEAD
  - storm_failover.py       : Resolver selector
  - storm_dns.py            : DNS transport
  - nexora_v2_integration.py: THIS FILE + Gateway/Carrier
  - test_nexora_v2_integration.py: Unit & integration tests

Nexora v2 files:
  - nexora/src/nexora_v2.py : CarrierManager, StreamMux
  - nexora/docs/V2_FLOW_STATE_MACHINE.md
  - nexora/docs/V2_PROTOCOL_SPEC.md

Testing:
  - test_local_dns_client.py : Local DNS server testing
  - test_real_resolvers.py   : Public resolver testing
  - test_throughput.py       : Performance measurement
"""
