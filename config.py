"""
STORM Configuration - Production setup

Copy this to config.py and customize for your environment.
"""

# ============================================================================
# DNS RESOLVERS
# ============================================================================

# List of public DNS resolvers
# These should support EDNS0 UDP extension
RESOLVERS = [
    "185.49.84.2",      # Primary resolver
    "178.22.122.100",   # Secondary resolver
    # "9.9.9.9",        # Quad9
    # "1.1.1.1",        # Cloudflare
    # "8.8.8.8",        # Google
]

# DNS zone for tunnel
TUNNEL_ZONE = "t1.phonexpress.ir"

# Query type (TXT recommended for larger payloads)
QUERY_TYPE = "TXT"

# ============================================================================
# CLIENT CONFIGURATION (Inside Gateway)
# ============================================================================

CLIENT_CONFIG = {
    # Listen address for local apps
    "listen_host": "127.0.0.1",
    "listen_port": 1443,
    
    # Connection configuration
    "connection": {
        "block_size": 16,  # FEC block size (packets)
        "keepalive_interval": 10.0,  # Seconds
        "keepalive_timeout": 5.0,  # Seconds
        "max_inflight": 64,  # Max unacked packets
        "reassembly_timeout": 30.0,  # Seconds
    },
    
    # Resolver selection
    "resolver": {
        "ewma_alpha": 0.2,  # Exponential moving average weight
        "blacklist_cooldown": 10.0,  # Seconds
        "fail_threshold": 3,  # Failures before blacklist
    },
    
    # Logging
    "log_level": "INFO",  # DEBUG, INFO, WARNING, ERROR
    "log_file": None,  # None = console only, or path to file
}

# ============================================================================
# SERVER CONFIGURATION (Outside Gateway)
# ============================================================================

SERVER_CONFIG = {
    # DNS listener
    "listen_host": "0.0.0.0",
    "listen_port": 53,
    
    # Target service (what to forward connections to)
    "target": {
        "host": "127.0.0.1",
        "port": 8443,  # Default: X-UI VLESS port
        # "host": "example.com",
        # "port": 443,
    },
    
    # Connection configuration (same as client)
    "connection": {
        "block_size": 16,
        "keepalive_interval": 10.0,
        "keepalive_timeout": 5.0,
        "max_inflight": 64,
        "reassembly_timeout": 30.0,
    },
    
    # Logging
    "log_level": "INFO",
    "log_file": None,
}

# ============================================================================
# ADVANCED OPTIONS
# ============================================================================

# FEC/Parity recovery
FEC_CONFIG = {
    "block_size": 16,  # Packets per block
    "enable_parity": True,  # Generate parity packets
    "parity_ratio": 0.25,  # 25% overhead (1 parity per 4 packets)
}

# DNS transport options
DNS_CONFIG = {
    "edns0_payload": 4096,  # EDNS0 UDP buffer size
    "query_timeout": 3.0,  # Seconds
    "retry_count": 2,  # Retry attempts
    "parallel_resolvers": 2,  # Send to N resolvers in parallel
}

# Performance tuning
PERFORMANCE = {
    "chunk_size": 200,  # Bytes per packet (tuned vs DNS limits)
    "max_concurrent_streams": 512,  # Connections per server
    "buffer_size_per_connection": 1048576,  # 1MB reassembly buffer
    "keepalive_interval": 10,  # Seconds
}

# ============================================================================
# DEPLOYMENT SCENARIOS
# ============================================================================

# Example: Inside server (client role)
INSIDE_SERVER_CONFIG = {
    "role": "client",
    "listen": ("0.0.0.0", 1443),  # Accept from any interface
    "zone": "t1.phonexpress.ir",
    # resolvers from RESOLVERS list above
}

# Example: Outside server (server role)
OUTSIDE_SERVER_CONFIG = {
    "role": "server",
    "listen": ("0.0.0.0", 53),  # Listen on UDP/53
    "target": ("127.0.0.1", 8443),  # Forward to X-UI
}

# ============================================================================
# SYSTEMD SERVICE TEMPLATE
# ============================================================================

"""
# /etc/systemd/system/storm-client.service
[Unit]
Description=STORM DNS Tunnel Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=storm
ExecStart=/usr/bin/python3 /opt/storm/storm_client.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

# /etc/systemd/system/storm-server.service
[Unit]
Description=STORM DNS Tunnel Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=storm
ExecStart=/usr/bin/python3 /opt/storm/storm_server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
"""

# ============================================================================
# MONITORING & METRICS
# ============================================================================

METRICS_CONFIG = {
    "enabled": True,
    "export_interval": 60,  # Seconds
    "export_target": None,  # Send metrics to URL
    "track": {
        "success_rate": True,
        "latency_percentiles": True,  # p50, p95, p99
        "resolver_health": True,
        "fec_recovery_rate": True,
        "active_connections": True,
    },
}

# ============================================================================
# SECURITY OPTIONS (Phase 2+)
# ============================================================================

SECURITY_CONFIG = {
    # Encryption (not implemented in Phase 1)
    "encryption": {
        "enabled": False,  # TODO: implement ChaCha20
        "algorithm": "ChaCha20-Poly1305",
        "pre_shared_key": None,  # Or derive from config
    },
    
    # Authentication
    "authentication": {
        "enabled": False,  # TODO: implement ed25519
        "public_key": None,
    },
    
    # Rate limiting
    "rate_limit": {
        "enabled": True,
        "queries_per_second": 500,
        "connections_per_minute": 100,
    },
}
