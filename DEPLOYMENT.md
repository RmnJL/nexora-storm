"""
STORM Phase 3 - Production Deployment Guide

Steps to deploy STORM with Nexora v2 integration in production.
"""

# ============================================================================
# PHASE 3 DEPLOYMENT PLAN
# ============================================================================

"""
OBJECTIVES:
  1. Deploy STORM as transport layer for Nexora v2
  2. Enable encrypted, resilient DNS tunneling
  3. Monitor and validate production performance
  4. Gracefully handle resolver failures

TIMELINE:
  - Pre-deployment: 30 minutes (setup, config)
  - Deployment: 15 minutes (start services)
  - Validation: 15 minutes (run tests)
  - Monitoring: Continuous

SCOPE:
  - Inside gateway: 5.210.94.207 (client)
  - Outside gateway: 185.97.116.13 (server)
  - Zone: t1.phonexpress.ir
  - Resolvers: 8.8.8.8, 1.1.1.1, 9.9.9.9
"""

# ============================================================================
# 1. PRE-DEPLOYMENT CHECKLIST
# ============================================================================

PRE_DEPLOYMENT_CHECKLIST = """
Before deploying to production:

INFRASTRUCTURE:
  ☐ Inside gateway (5.210.94.207) accessible
  ☐ Outside gateway (185.97.116.13) accessible
  ☐ DNS zone: t1.phonexpress.ir created and delegated
  ☐ Port 53 (UDP) available on outside gateway
  ☐ Port 5000+ (TCP) available for SOCKS5 on inside gateway

DEPENDENCIES:
  ☐ Python 3.8+ installed on both gateways
  ☐ pip/poetry installed
  ☐ Requirements installed: dnspython, cryptography

CODE:
  ☐ All files copied to production servers
  ☐ test_nexora_v2_integration.py passes locally
  ☐ test_real_resolvers.py passes with target resolvers
  ☐ benchmark.py runs without errors

CONFIGURATION:
  ☐ config.py reviewed and updated
  ☐ Encryption PSK generated (32 bytes)
  ☐ Resolver list verified
  ☐ Max carriers appropriate for expected load

MONITORING:
  ☐ Logging configured
  ☐ Metrics collection set up
  ☐ Alerts configured (if using)
"""

# ============================================================================
# 2. CONFIGURATION SETUP
# ============================================================================

CONFIGURATION_TEMPLATE = """
From config.py:

STORM Configuration
─────────────────────

# DNS zone for tunnel
DNS_ZONE = "t1.phonexpress.ir"

# Resolvers to use (order = preference)
RESOLVERS = [
    "8.8.8.8",      # Google (reliable)
    "1.1.1.1",      # Cloudflare (fast)
    "9.9.9.9",      # Quad9 (privacy)
]

# Encryption (30+ bytes for security)
ENCRYPTION_PSK = b"your-32-byte-preshared-key-here!!"

# Connection settings
CARRIER_CONFIG = {
    "max_carriers": 3,           # Limit concurrent connections
    "keepalive_interval": 10.0,  # Seconds
    "rto_initial": 1.0,          # Initial RTO
    "rto_max": 30.0,             # Max RTO
}

# Logging
DEBUG = False                    # Set True for verbose logs
METRICS_EXPORT_INTERVAL = 30.0  # Seconds between exports

Example values:
- DNS_ZONE: Updated to match actual zone
- RESOLVERS: Regional resolvers if needed
- ENCRYPTION_PSK: Generate via: python -c 'import secrets; print(secrets.token_bytes(32))'
- CARRIER_CONFIG: Adjust max_carriers based on concurrent users
"""

# ============================================================================
# 3. DEPLOYMENT STEPS - OUTSIDE GATEWAY (185.97.116.13)
# ============================================================================

OUTSIDE_GATEWAY_STEPS = """
OUTSIDE GATEWAY DEPLOYMENT (185.97.116.13)
═══════════════════════════════════════════

Step 1: Setup environment
─────────────────────────
  $ ssh root@185.97.116.13
  
  $ apt update && apt install -y python3 python3-pip
  $ pip3 install dnspython cryptography
  
  $ mkdir -p /opt/storm
  $ cd /opt/storm

Step 2: Copy files
──────────────────
  From local:
  $ scp -r storm_*.py config.py test_*.py root@185.97.116.13:/opt/storm/
  $ scp NEXORA_V2_INTEGRATION_GUIDE.md root@185.97.116.13:/opt/storm/

Step 3: Configure
─────────────────
  Edit config.py:
  $ nano config.py
  
  Update:
  - DNS_ZONE: "t1.phonexpress.ir"
  - RESOLVERS: ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
  - ENCRYPTION_PSK: Generate new key
  - Role: "outside_gateway"

Step 4: Test locally
────────────────────
  $ python3 -m pytest test_nexora_v2_integration.py::TestSTORMNexoraV2Gateway::test_gateway_creation -v
  
  Expected: PASS

Step 5: Start DNS server (systemd)
──────────────────────────────────
  Create /etc/systemd/system/storm-dns.service:
  
  [Unit]
  Description=STORM DNS Server
  After=network.target
  
  [Service]
  Type=simple
  User=storm
  WorkingDirectory=/opt/storm
  ExecStart=/usr/bin/python3 storm_dns_server.py --zone t1.phonexpress.ir --listen 0.0.0.0:53
  Restart=always
  RestartSec=10
  
  [Install]
  WantedBy=multi-user.target
  
  Then:
  $ useradd -m storm
  $ systemctl daemon-reload
  $ systemctl enable storm-dns
  $ systemctl start storm-dns
  
  Verify:
  $ systemctl status storm-dns
  $ dig @185.97.116.13 example.com

Step 6: Enable packet forwarding
───────────────────────────────
  For transparent proxying (if needed):
  $ sysctl -w net.ipv4.ip_forward=1
  $ sysctl -w net.ipv4.conf.all.rp_filter=0

Step 7: Monitor
───────────────
  $ tail -f /var/log/syslog | grep storm-dns
  
  Or from Python:
  $ python3 << 'EOF'
  import asyncio
  from storm_dns_server import STORMDNSServer
  
  async def main():
      server = STORMDNSServer(zone="t1.phonexpress.ir")
      await server.start()
  
  asyncio.run(main())
  EOF
"""

# ============================================================================
# 4. DEPLOYMENT STEPS - INSIDE GATEWAY (5.210.94.207)
# ============================================================================

INSIDE_GATEWAY_STEPS = """
INSIDE GATEWAY DEPLOYMENT (5.210.94.207)
═════════════════════════════════════════

Step 1: Setup environment
─────────────────────────
  $ ssh root@5.210.94.207
  
  $ apt update && apt install -y python3 python3-pip
  $ pip3 install dnspython cryptography
  
  $ mkdir -p /opt/storm
  $ cd /opt/storm

Step 2: Copy files
──────────────────
  From local:
  $ scp -r storm_*.py nexora_v2_integration.py config.py root@5.210.94.207:/opt/storm/

Step 3: Configure
─────────────────
  Edit config.py:
  $ nano config.py
  
  Update:
  - DNS_ZONE: "t1.phonexpress.ir"
  - RESOLVERS: ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
  - ENCRYPTION_PSK: SAME as outside gateway!
  - Role: "inside_gateway"
  - OUTSIDE_SERVER: "185.97.116.13:53"

Step 4: Create STORM client daemon
──────────────────────────────────
  Create /opt/storm/storm_client_daemon.py:
  
  #!/usr/bin/env python3
  import asyncio
  import sys
  from nexora_v2_integration import STORMNexoraV2Gateway
  from config import RESOLVERS, ENCRYPTION_PSK, DNS_ZONE
  
  async def main():
      gateway = STORMNexoraV2Gateway(
          resolvers=RESOLVERS,
          zone=DNS_ZONE,
      )
      
      try:
          # Get carrier
          carrier = await gateway.get_or_create_carrier()
          print(f"✅ Connected! Carrier {carrier.carrier_id} ready")
          
          # Monitor
          while True:
              stats = gateway.get_stats()
              print(f"📊 Stats: {stats}")
              await asyncio.sleep(30)
      
      finally:
          await gateway.close()
  
  if __name__ == "__main__":
      asyncio.run(main())
  
  Then:
  $ chmod +x /opt/storm/storm_client_daemon.py

Step 5: Start client daemon (systemd)
─────────────────────────────────────
  Create /etc/systemd/system/storm-client.service:
  
  [Unit]
  Description=STORM Client Gateway
  After=network.target
  
  [Service]
  Type=simple
  User=storm
  WorkingDirectory=/opt/storm
  ExecStart=/usr/bin/python3 storm_client_daemon.py
  Restart=always
  RestartSec=10
  Environment="PYTHONUNBUFFERED=1"
  
  [Install]
  WantedBy=multi-user.target
  
  Then:
  $ useradd -m storm
  $ systemctl daemon-reload
  $ systemctl enable storm-client
  $ systemctl start storm-client

Step 6: Create SOCKS5 proxy (for applications)
───────────────────────────────────────────────
  Applications can connect via:
  
  export SOCKS5_PROXY=socks5://127.0.0.1:1080
  curl -x $SOCKS5_PROXY http://example.com
  
  Or create advanced SOCKS5 proxy using storm_client.py:
  
  $ python3 storm_client.py --listen 127.0.0.1:1080

Step 7: Verify connectivity
───────────────────────────
  $ python3 << 'EOF'
  import asyncio
  from nexora_v2_integration import STORMNexoraV2Gateway
  
  async def test():
      gateway = STORMNexoraV2Gateway(resolvers=["8.8.8.8", "1.1.1.1"])
      carrier = await gateway.get_or_create_carrier()
      stats = carrier.get_stats()
      print(f"✅ Connected: {stats}")
      await gateway.close()
  
  asyncio.run(test())
  EOF
"""

# ============================================================================
# 5. VALIDATION & TESTING
# ============================================================================

VALIDATION_STEPS = """
VALIDATION AND TESTING
══════════════════════

From a separate machine or CI environment:

Step 1: Test outside DNS server
──────────────────────────────
  $ dig @185.97.116.13 example.com
  
  Expected:
  - NOERROR status
  - Answer section present
  
  If fails:
  - Check firewall allows UDP/53
  - Verify DNS server running: systemctl status storm-dns
  - Check zone file: debug in storm_dns_server.py

Step 2: Test real resolvers
──────────────────────────
  $ cd /opt/storm
  $ python3 test_real_resolvers.py --mode=quick
  
  Expected output:
  ✅ 8.8.8.8: 5/5 queries successful (latency: ~50ms)
  ✅ 1.1.1.1: 5/5 queries successful (latency: ~40ms)
  ✅ 9.9.9.9: 5/5 queries successful (latency: ~60ms)

Step 3: Test local DNS client
─────────────────────────────
  Terminal 1 (outside):
  $ python3 storm_dns_server.py
  
  Terminal 2 (local):
  $ python3 test_local_dns_client.py
  
  Expected:
  ✅ Single packet test: OK
  ✅ Multiple packets: OK
  ✅ Large packet: OK

Step 4: Test integration
────────────────────────
  $ python3 test_nexora_v2_integration.py
  
  Expected: All tests pass
  - CarrierCreation: PASS
  - StreamMultiplexing: PASS
  - ResolverFailover: PASS
  - EncryptionIntegration: PASS

Step 5: Throughput benchmark
────────────────────────────
  $ python3 benchmark.py
  
  Expected output:
  - Encryption: 100-1000 Mbps logical throughput
  - Packet building: sub-millisecond
  - Resolver selection: microseconds
  - Gateway throughput: 1-5 Mbps (DNS-limited)

Step 6: End-to-end tunnel test
──────────────────────────────
  Inside gateway:
  $ python3 storm_client.py --listen 127.0.0.1:1080 --target 185.97.116.13:53
  
  Outside gateway:
  $ python3 storm_dns_server.py --zone t1.phonexpress.ir
  
  Test connection:
  $ curl -x socks5://127.0.0.1:1080 http://example.com
  
  Expected: Web content retrieved through STORM tunnel
"""

# ============================================================================
# 6. MONITORING & TROUBLESHOOTING
# ============================================================================

MONITORING_TEMPLATE = """
PRODUCTION MONITORING
═════════════════════

Key Metrics to Track:
  - Packet success rate (target: >99%)
  - FEC recovery rate (target: <5% packets need recovery)
  - Resolver health scores (target: all >0.9)
  - Connection uptime (target: 99.9%+)
  - Latency p95 (target: <200ms)

Create monitoring script:

  #!/usr/bin/env python3
  import asyncio
  import json
  import time
  from datetime import datetime
  from nexora_v2_integration import STORMNexoraV2Gateway
  
  async def monitor():
      gateway = STORMNexoraV2Gateway(resolvers=["8.8.8.8", "1.1.1.1", "9.9.9.9"])
      
      while True:
          stats = gateway.get_stats()
          
          # Export to JSON
          with open("/var/log/storm_metrics.json", "a") as f:
              f.write(json.dumps({
                  "timestamp": datetime.now().isoformat(),
                  "stats": stats,
              }) + "\\n")
          
          # Print summary
          for cs in stats['carrier_stats']:
              sent = cs['packets_sent']
              recv = cs['packets_received']
              success_rate = recv/sent*100 if sent > 0 else 0
              print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"Carrier {cs['carrier_id']}: "
                    f"{success_rate:.1f}% ({recv}/{sent})")
          
          await asyncio.sleep(30)
  
  asyncio.run(monitor())

Common Issues and Fixes:

  Issue: High packet loss (< 95% success rate)
  ──────────────────────────────────────────
  Fixes:
    1. Check resolver health: test_real_resolvers.py
    2. Increase RTO: config.rto_initial = 2.0
    3. Try different resolvers
    4. Check DNS server logs

  Issue: Connection drops after ~40s
  ──────────────────────────────────
  Fixes:
    1. Verify keepalive enabled (default: yes)
    2. Increase connection timeout
    3. Check for DNS server restart

  Issue: Asymmetric latency
  ────────────────────────
  Fixes:
    1. Check resolver locations (geographic)
    2. Use regional resolvers
    3. Adjust dual resolver timeout

  Issue: Memory leak
  ─────────────────
  Fixes:
    1. Check carrier limit: max_carriers in config
    2. Monitor garbage collection
    3. Restart periodically if needed

  Issue: Encryption errors
  ───────────────────────
  Fixes:
    1. Verify PSK matches both endpoints
    2. Check for packet corruption
    3. Verify conn_id is consistent
"""

# ============================================================================
# 7. ROLLBACK PLAN
# ============================================================================

ROLLBACK_PLAN = """
ROLLBACK / FALLBACK PLAN
════════════════════════

If STORM deployment has issues:

Quick Disable (1 minute):
  Outside:  systemctl stop storm-dns
  Inside:   systemctl stop storm-client
  
  Revert to Nexora v1 DNS or SSH tunnel

Clean Rollback (5 minutes):
  1. Stop services:
     $ systemctl stop storm-dns
     $ systemctl stop storm-client
  
  2. Disable startup:
     $ systemctl disable storm-dns
     $ systemctl disable storm-client
  
  3. Restore previous config:
     $ cp /opt/storm/config.py.bak /opt/storm/config.py
  
  4. Restart applications

Issues Requiring Rollback:
  - Carrier creation failing consistently
  - Encryption key mismatch
  - DNS zone not resolving
  - Packet loss > 10%
  - Connection drops every <5 minutes

Recovery Steps:
  1. Identify root cause (check logs)
  2. Fix on dev environment
  3. Test locally: test_nexora_v2_integration.py
  4. Re-deploy with fixes
  5. Gradual rollout: inside gateway first, then outside
"""

# ============================================================================
# 8. PHASE 4 PLANNING
# ============================================================================

PHASE_4_PLANNING = """
PHASE 4 - POST-DEPLOYMENT (Optional)
════════════════════════════════════

After Phase 3 (STORM + Nexora v2) is live:

Metrics & Monitoring (Week 1):
  □ Set up Prometheus exporter
  □ Create Grafana dashboard
  □ Configure alerts
  □ Monitor 24/7

Security Hardening (Week 1-2):
  □ Add ed25519 authentication
  □ Implement DOS rate limiting
  □ Audit encryption implementation
  □ Penetration test

Performance Optimization (Week 2-3):
  □ Measure real throughput
  □ Tune FEC block size
  □ Optimize resolver selection
  □ Regional clustering

Load Testing (Week 3):
  □ Test with increasing concurrent streams
  □ Measure max throughput
  □ Identify bottlenecks
  □ Optimize for production load

Production Hardening (Week 4+):
  □ Implement advanced congestion control
  □ Add multi-region failover
  □ Implement metrics export
  □ Full security audit
"""

# ============================================================================
# DEPLOYMENT WORKFLOW
# ============================================================================

DEPLOYMENT_WORKFLOW = """
RECOMMENDED DEPLOYMENT WORKFLOW
════════════════════════════════

Timeline: ~2 hours total

T+0:00 - Pre-deployment
  ☐ Run checklist
  ☐ Review config
  ☐ Backup existing config

T+0:15 - Outside Gateway
  ☐ SSH to 185.97.116.13
  ☐ Copy files
  ☐ Configure
  ☐ Test locally (test_real_resolvers.py)
  ☐ Start service: systemctl start storm-dns
  ☐ Verify: dig @185.97.116.13

T+0:45 - Inside Gateway
  ☐ SSH to 5.210.94.207
  ☐ Copy files
  ☐ Configure (SAME PSK, SAME RESOLVERS)
  ☐ Test locally
  ☐ Start service: systemctl start storm-client

T+1:00 - Validation
  ☐ test_real_resolvers.py --mode=full
  ☐ test_nexora_v2_integration.py
  ☐ benchmark.py
  ☐ End-to-end tunnel test

T+1:30 - Monitoring Setup
  ☐ Start monitoring script
  ☐ Verify metrics collection
  ☐ Configure alerts

T+2:00 - Done!
  ☐ Document deployment
  ☐ Archive configs
  ☐ Hand off to ops team

POST-DEPLOYMENT:
  - Monitor for 24 hours
  - Check metrics hourly
  - Document lessons learned
  - Plan Phase 4 improvements
"""

print(__doc__)

if __name__ == "__main__":
    print("\n".join([
        PRE_DEPLOYMENT_CHECKLIST,
        "\n",
        CONFIGURATION_TEMPLATE,
        "\n",
        OUTSIDE_GATEWAY_STEPS,
        "\n",
        INSIDE_GATEWAY_STEPS,
        "\n",
        VALIDATION_STEPS,
        "\n",
        MONITORING_TEMPLATE,
        "\n",
        ROLLBACK_PLAN,
        "\n",
        PHASE_4_PLANNING,
        "\n",
        DEPLOYMENT_WORKFLOW,
    ]))
