#!/usr/bin/env python3
"""
STORM Deployment Helper Script

Automates setup and validation for Phase 3 deployment.
"""

import asyncio
import subprocess
import sys
import os
from pathlib import Path
from typing import List, Tuple

class DeploymentHelper:
    """Helper for STORM deployment"""
    
    def __init__(self, mode: str = "local"):
        self.mode = mode  # local | outside | inside
        self.checks_passed = []
        self.checks_failed = []
    
    def run(self, cmd: str, check: bool = True) -> Tuple[int, str, str]:
        """Run shell command"""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timeout"
        except Exception as e:
            return -1, "", str(e)
    
    def check(self, name: str, cmd: str) -> bool:
        """Run check"""
        print(f"  Checking: {name}...", end=" ", flush=True)
        code, stdout, stderr = self.run(cmd, check=False)
        
        if code == 0:
            print("✅")
            self.checks_passed.append(name)
            return True
        else:
            print(f"❌ ({stderr.strip()})")
            self.checks_failed.append((name, stderr))
            return False
    
    # ========================================================================
    # VALIDATION CHECKS
    # ========================================================================
    
    def validate_python(self) -> bool:
        """Validate Python version"""
        return self.check("Python 3.8+", "python3 --version | grep -E 'Python 3\\.([89]|1[0-9])'")
    
    def validate_pip(self) -> bool:
        """Validate pip"""
        return self.check("pip3", "pip3 --version")
    
    def validate_dependencies(self) -> bool:
        """Validate dependencies"""
        deps = ["dnspython", "cryptography"]
        all_ok = True
        
        for dep in deps:
            code, _, _ = self.run(f"python3 -c 'import {dep}'", check=False)
            ok = code == 0
            status = "✅" if ok else "❌"
            print(f"    {status} {dep}")
            all_ok = all_ok and ok
        
        return all_ok
    
    def validate_files(self) -> bool:
        """Validate STORM files exist"""
        required_files = [
            "storm_proto.py",
            "storm_encryption.py",
            "storm_connection.py",
            "nexora_v2_integration.py",
            "test_nexora_v2_integration.py",
            "test_real_resolvers.py",
            "benchmark.py",
        ]
        
        all_ok = True
        for fname in required_files:
            exists = Path(fname).exists()
            status = "✅" if exists else "❌"
            print(f"    {status} {fname}")
            all_ok = all_ok and exists
        
        return all_ok
    
    def validate_network(self) -> bool:
        """Validate network connectivity"""
        all_ok = True
        
        resolvers = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
        for resolver in resolvers:
            code, _, _ = self.run(f"timeout 2 ping -c 1 {resolver}", check=False)
            ok = code == 0
            status = "✅" if ok else "❌"
            print(f"    {status} {resolver}")
            all_ok = all_ok and ok
        
        return all_ok
    
    # ========================================================================
    # TESTS
    # ========================================================================
    
    async def test_encryption(self) -> bool:
        """Test encryption module"""
        print("  Testing encryption...", end=" ", flush=True)
        
        try:
            from storm_encryption import STORMCrypto
            
            plaintext = b"test data"
            nonce, ct = STORMCrypto.encrypt_packet(plaintext, b"test", 1, b"psk")
            recovered = STORMCrypto.decrypt_packet(nonce, ct, b"test", 1, b"psk")
            
            if recovered == plaintext:
                print("✅")
                return True
            else:
                print("❌ (decrypt mismatch)")
                return False
        except Exception as e:
            print(f"❌ ({e})")
            return False
    
    async def test_carrier(self) -> bool:
        """Test carrier creation"""
        print("  Testing carrier creation...", end=" ", flush=True)
        
        try:
            from nexora_v2_integration import STORMCarrier, CarrierConfig
            
            carrier = STORMCarrier(
                carrier_id=1,
                resolvers=["8.8.8.8"],
                config=CarrierConfig(),
            )
            
            await carrier.start()
            stats = carrier.get_stats()
            await carrier.close()
            
            if stats["carrier_id"] == 1 and not stats["active"]:
                print("✅")
                return True
            else:
                print("❌ (invalid state)")
                return False
        except Exception as e:
            print(f"❌ ({e})")
            return False
    
    async def test_gateway(self) -> bool:
        """Test gateway"""
        print("  Testing gateway...", end=" ", flush=True)
        
        try:
            from nexora_v2_integration import STORMNexoraV2Gateway
            
            gateway = STORMNexoraV2Gateway(resolvers=["8.8.8.8"])
            carrier = await gateway.get_or_create_carrier()
            stats = gateway.get_stats()
            await gateway.close()
            
            if stats["carriers"] >= 1:
                print("✅")
                return True
            else:
                print("❌ (no carriers)")
                return False
        except Exception as e:
            print(f"❌ ({e})")
            return False
    
    # ========================================================================
    # MAIN WORKFLOW
    # ========================================================================
    
    async def validate_local(self):
        """Validate local environment"""
        print("\n" + "="*60)
        print("LOCAL ENVIRONMENT VALIDATION")
        print("="*60)
        
        # Basic checks
        print("\n1. Basic Requirements:")
        self.validate_python()
        self.validate_pip()
        
        print("\n2. Dependencies:")
        self.validate_dependencies()
        
        print("\n3. Files:")
        self.validate_files()
        
        print("\n4. Network:")
        self.validate_network()
        
        # Tests
        print("\n5. Functional Tests:")
        await self.test_encryption()
        await self.test_carrier()
        await self.test_gateway()
        
        # Summary
        print("\n" + "="*60)
        print(f"Passed: {len(self.checks_passed)}")
        print(f"Failed: {len(self.checks_failed)}")
        
        if self.checks_failed:
            print("\nFailed checks:")
            for name, error in self.checks_failed:
                print(f"  ❌ {name}: {error.strip()}")
            return False
        else:
            print("\n✅ All checks passed! Ready for deployment.")
            return True
    
    def run_tests(self):
        """Run test suite"""
        print("\n" + "="*60)
        print("RUNNING TEST SUITE")
        print("="*60)
        
        tests = [
            ("Unit tests", "python3 test_nexora_v2_integration.py"),
            ("Integration tests", "python3 test_real_resolvers.py --mode=quick"),
            ("Benchmarks", "python3 benchmark.py"),
        ]
        
        for name, cmd in tests:
            print(f"\n{name}...")
            code, stdout, stderr = self.run(cmd, check=False)
            
            if code == 0:
                print(f"✅ {name} passed")
            else:
                print(f"❌ {name} failed")
                if stderr:
                    print(f"  Error: {stderr[:200]}")
    
    def generate_config(self):
        """Generate config file"""
        print("\n" + "="*60)
        print("GENERATING CONFIGURATION")
        print("="*60)
        
        if not Path("config.py").exists():
            print("\nNo config.py found. Creating template...")
            
            config_template = '''"""
STORM Configuration

Edit this file for your deployment.
"""

# DNS zone for tunnel
DNS_ZONE = "t1.phonexpress.ir"

# Resolvers to use (order = preference)
RESOLVERS = [
    "8.8.8.8",      # Google
    "1.1.1.1",      # Cloudflare
    "9.9.9.9",      # Quad9
]

# Encryption PSK (must be 32+ bytes)
# Generate: python3 -c 'import secrets; print(secrets.token_hex(16))'
ENCRYPTION_PSK = b"change-me-to-a-real-32-byte-key"

# Deployment role
ROLE = "local"  # local | inside_gateway | outside_gateway

# Carrier configuration
CARRIER_CONFIG = {
    "max_carriers": 3,
    "keepalive_interval": 10.0,
    "rto_initial": 1.0,
    "rto_max": 30.0,
}

# Logging
DEBUG = False
METRICS_EXPORT_INTERVAL = 30.0

# DNS Server (outside gateway)
DNS_LISTEN_HOST = "0.0.0.0"
DNS_LISTEN_PORT = 53

# SOCKS5 Proxy (inside gateway)
SOCKS5_LISTEN_HOST = "127.0.0.1"
SOCKS5_LISTEN_PORT = 1080
'''
            
            with open("config.py", "w") as f:
                f.write(config_template)
            
            print("✅ Created config.py (edit before deployment!)")
        else:
            print("✅ config.py already exists")


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="STORM Deployment Helper")
    parser.add_argument("--validate", action="store_true", help="Validate environment")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--config", action="store_true", help="Generate config file")
    parser.add_argument("--quick", action="store_true", help="Quick validation only")
    
    args = parser.parse_args()
    
    helper = DeploymentHelper(mode="local")
    
    if args.config:
        helper.generate_config()
    
    if args.validate or args.quick or (not args.test):
        # Run async validation
        try:
            result = asyncio.run(helper.validate_local())
            if not result and not args.quick:
                sys.exit(1)
        except Exception as e:
            print(f"\n❌ Error: {e}")
            sys.exit(1)
    
    if args.test:
        helper.run_tests()
    
    print("\n✅ Deployment helper completed")


if __name__ == "__main__":
    main()
