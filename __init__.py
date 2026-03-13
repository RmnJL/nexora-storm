"""
STORM - Stateful Tunnel Over Resilient Messaging

A resilient DNS-based tunnel protocol for stable, multi-resolver transport.

Author: Rmn JL
"""

__version__ = "0.1.0"
__author__ = "Rmn JL"

from storm_proto import (
    MAGIC,
    VERSION,
    PacketFlags,
    FrameType,
    make_packet,
    parse_packet,
    make_frame,
    parse_frame,
)

from storm_fec import FECRecovery, ParityBlock, compute_parity

from storm_failover import ResolverSelector, ResolverHealth

from storm_connection import (
    STORMConnection,
    ConnectionManager,
    ConnectionConfig,
    ConnectionState,
)

from storm_dns import DNSTransport, DNSGateway

from storm_client import STORMClient
from storm_server import STORMServer, TargetConnector

__all__ = [
    "MAGIC",
    "VERSION",
    "PacketFlags",
    "FrameType",
    "make_packet",
    "parse_packet",
    "make_frame",
    "parse_frame",
    "FECRecovery",
    "ParityBlock",
    "compute_parity",
    "ResolverSelector",
    "ResolverHealth",
    "STORMConnection",
    "ConnectionManager",
    "ConnectionConfig",
    "ConnectionState",
    "DNSTransport",
    "DNSGateway",
    "STORMClient",
    "STORMServer",
    "TargetConnector",
]
