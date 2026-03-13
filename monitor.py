#!/usr/bin/env python3
"""
STORM Production Monitoring & Health Check Script

Continuous monitoring of STORM gateway health and performance.
"""

import asyncio
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("/var/log/storm_monitor.log"),
        logging.StreamHandler(),
    ]
)

log = logging.getLogger("storm-monitor")


class STORMHealthMonitor:
    """Monitor STORM gateway health"""
    
    def __init__(
        self,
        poll_interval: float = 30.0,
        metrics_file: str = "/var/log/storm_metrics.json",
    ):
        self.poll_interval = poll_interval
        self.metrics_file = metrics_file
        
        self.metrics_history: List[Dict] = []
        self.alerts: List[str] = []
        
        # Thresholds
        self.success_rate_threshold = 0.95  # 95%
        self.latency_threshold_ms = 200.0
        self.carrier_count_max = 5
    
    async def collect_metrics(self, gateway) -> Dict:
        """Collect current metrics from gateway"""
        
        stats = gateway.get_stats()
        timestamp = datetime.now().isoformat()
        
        metrics = {
            "timestamp": timestamp,
            "carriers": stats.get("carriers", 0),
            "carrier_details": [],
        }
        
        total_sent = 0
        total_received = 0
        
        for cs in stats.get("carrier_stats", []):
            carrier_id = cs["carrier_id"]
            sent = cs["packets_sent"]
            received = cs["packets_received"]
            active = cs["active"]
            
            success_rate = received / sent if sent > 0 else 0
            
            carrier_metric = {
                "carrier_id": carrier_id,
                "packets_sent": sent,
                "packets_received": received,
                "success_rate": success_rate,
                "active": active,
            }
            
            metrics["carrier_details"].append(carrier_metric)
            total_sent += sent
            total_received += received
        
        # Aggregate metrics
        metrics["total_packets_sent"] = total_sent
        metrics["total_packets_received"] = total_received
        metrics["overall_success_rate"] = total_received / total_sent if total_sent > 0 else 0
        
        return metrics
    
    def check_health(self, metrics: Dict) -> List[str]:
        """Check gateway health and return alerts"""
        
        alerts = []
        
        # Check carrier count
        carrier_count = metrics.get("carriers", 0)
        if carrier_count > self.carrier_count_max:
            alerts.append(f"⚠️  High carrier count: {carrier_count} (max: {self.carrier_count_max})")
        elif carrier_count == 0:
            alerts.append("🚨 No active carriers!")
        
        # Check overall success rate
        success_rate = metrics.get("overall_success_rate", 0)
        if success_rate < self.success_rate_threshold:
            alerts.append(f"🚨 Low success rate: {success_rate*100:.1f}% (threshold: {self.success_rate_threshold*100:.0f}%)")
        
        # Check per-carrier health
        for cs in metrics.get("carrier_details", []):
            if cs["active"] and cs["success_rate"] < 0.90:
                alerts.append(
                    f"⚠️  Carrier {cs['carrier_id']} degraded: "
                    f"{cs['success_rate']*100:.1f}% success"
                )
        
        return alerts
    
    def export_metrics(self, metrics: Dict):
        """Export metrics to JSON file"""
        
        try:
            # Append to metrics file
            with open(self.metrics_file, "a") as f:
                f.write(json.dumps(metrics) + "\n")
            
            self.metrics_history.append(metrics)
            
            # Keep only last 1440 entries (24 hours at 1-minute intervals)
            if len(self.metrics_history) > 1440:
                self.metrics_history = self.metrics_history[-1440:]
        
        except Exception as e:
            log.error(f"Failed to export metrics: {e}")
    
    def print_status(self, metrics: Dict, alerts: List[str]):
        """Print current status"""
        
        timestamp = metrics.get("timestamp", "?")
        carrier_count = metrics.get("carriers", 0)
        total_sent = metrics.get("total_packets_sent", 0)
        total_received = metrics.get("total_packets_received", 0)
        success_rate = metrics.get("overall_success_rate", 0)
        
        # Status line
        print(f"\n[{timestamp}] STORM Gateway Status")
        print("─" * 60)
        
        # Carrier info
        print(f"Carriers:        {carrier_count}")
        print(f"Total packets:   {total_sent} sent, {total_received} received")
        print(f"Success rate:    {success_rate*100:.1f}%")
        print()
        
        # Carrier details
        if metrics.get("carrier_details"):
            print("Carrier Details:")
            for cs in metrics["carrier_details"]:
                cid = cs["carrier_id"]
                sent = cs["packets_sent"]
                recv = cs["packets_received"]
                rate = cs["success_rate"]
                active = "✓" if cs["active"] else "✗"
                
                print(f"  [{active}] Carrier {cid}: {rate*100:.1f}% ({recv}/{sent})")
        
        # Alerts
        if alerts:
            print("\nAlerts:")
            for alert in alerts:
                print(f"  {alert}")
        else:
            print("\n✅ All systems normal")
        
        print("─" * 60)
    
    async def monitor_loop(self, gateway):
        """Main monitoring loop"""
        
        log.info("Starting STORM health monitor")
        print("\n" + "="*60)
        print("STORM PRODUCTION MONITOR")
        print("="*60)
        print(f"Poll interval: {self.poll_interval}s")
        print(f"Metrics file: {self.metrics_file}")
        print()
        
        iteration = 0
        
        try:
            while True:
                iteration += 1
                
                try:
                    # Collect metrics
                    metrics = await self.collect_metrics(gateway)
                    
                    # Check health
                    alerts = self.check_health(metrics)
                    
                    # Export
                    self.export_metrics(metrics)
                    
                    # Print status
                    self.print_status(metrics, alerts)
                    
                    # Log alerts
                    if alerts:
                        for alert in alerts:
                            log.warning(alert)
                    
                    # Extra metrics
                    if iteration % 10 == 0:  # Every 5 minutes (30s * 10)
                        await self.print_summary()
                
                except Exception as e:
                    log.error(f"Error in monitoring loop: {e}")
                
                # Sleep until next poll
                await asyncio.sleep(self.poll_interval)
        
        except KeyboardInterrupt:
            log.info("Monitor stopped by user")
            print("\n✅ Monitor stopped")
    
    async def print_summary(self):
        """Print monitoring summary"""
        
        if not self.metrics_history:
            return
        
        print("\n📊 SUMMARY (last hour):")
        
        # Calculate statistics
        success_rates = [m.get("overall_success_rate", 0) for m in self.metrics_history[-120:]]  # 1 hour
        
        if success_rates:
            avg_rate = sum(success_rates) / len(success_rates)
            min_rate = min(success_rates)
            max_rate = max(success_rates)
            
            print(f"  Success rate: avg={avg_rate*100:.1f}%, min={min_rate*100:.1f}%, max={max_rate*100:.1f}%")
        
        # Carrier uptime
        if self.metrics_history:
            recent = self.metrics_history[-1]
            print(f"  Current carriers: {recent.get('carriers', 0)}")
            print(f"  Packets processed: {recent.get('total_packets_sent', 0)}")


async def main(args):
    """Main entry point"""
    
    try:
        from nexora_v2_integration import STORMNexoraV2Gateway
    except ImportError:
        print("❌ STORM modules not found. Run from project directory.")
        sys.exit(1)
    
    # Parse arguments
    poll_interval = float(getattr(args, "interval", 30))
    metrics_file = getattr(args, "metrics", "/var/log/storm_metrics.json")
    resolvers = getattr(args, "resolvers", ["8.8.8.8", "1.1.1.1", "9.9.9.9"])
    
    if isinstance(resolvers, str):
        resolvers = resolvers.split(",")
    
    # Create gateway
    gateway = STORMNexoraV2Gateway(resolvers=resolvers)
    
    # Create monitor
    monitor = STORMHealthMonitor(
        poll_interval=poll_interval,
        metrics_file=metrics_file,
    )
    
    try:
        # Start monitoring
        await monitor.monitor_loop(gateway)
    finally:
        await gateway.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="STORM Production Monitor")
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Poll interval in seconds (default: 30)"
    )
    parser.add_argument(
        "--metrics",
        type=str,
        default="/var/log/storm_metrics.json",
        help="Metrics output file (default: /var/log/storm_metrics.json)"
    )
    parser.add_argument(
        "--resolvers",
        type=str,
        default="8.8.8.8,1.1.1.1,9.9.9.9",
        help="Comma-separated resolver list"
    )
    
    args = parser.parse_args()
    
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\n✅ Monitor stopped")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
