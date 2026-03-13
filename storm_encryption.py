"""
STORM Encryption Module - Per-packet ChaCha20-Poly1305 AEAD

Provides encryption layer for STORM protocol.
"""

from __future__ import annotations

import os
import hashlib
import secrets
from typing import Tuple, Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


class EncryptionKey:
    """Encryption key management"""
    
    def __init__(self, pre_shared_key: Optional[bytes] = None):
        """
        Initialize encryption key.
        
        Args:
            pre_shared_key: Optional 32-byte pre-shared key. If None, generates random.
        """
        if pre_shared_key:
            if len(pre_shared_key) != 32:
                raise ValueError("PSK must be 32 bytes")
            self.key = pre_shared_key
        else:
            self.key = secrets.token_bytes(32)
    
    @staticmethod
    def from_password(password: str, salt: Optional[bytes] = None) -> EncryptionKey:
        """Derive key from password using HKDF"""
        if not salt:
            salt = secrets.token_bytes(16)
        
        kdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"STORM-KEY-DERIVATION",
        )
        
        key = kdf.derive(password.encode())
        return EncryptionKey(key)
    
    def get_packet_key(self, conn_id: bytes, seq: int) -> bytes:
        """Derive per-packet key from conn_id and sequence"""
        info = conn_id + seq.to_bytes(4, 'big')
        kdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.key,
            info=info,
        )
        return kdf.derive(self.key)


class STORMCrypto:
    """Encryption/decryption for STORM packets"""
    
    def __init__(self, psk: Optional[bytes] = None):
        """Initialize with optional pre-shared key"""
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography library not installed")
        
        self.key = EncryptionKey(psk)
    
    @staticmethod
    def encrypt_packet(
        plaintext: bytes,
        conn_id: bytes,
        seq: int,
        psk: Optional[bytes] = None,
    ) -> Tuple[bytes, bytes]:
        """
        Encrypt packet data.
        
        Returns: (nonce, ciphertext)
        """
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography library not installed")
        
        # Generate random nonce
        nonce = secrets.token_bytes(12)  # ChaCha20 uses 12-byte nonce
        
        # Derive packet key
        key_mgr = EncryptionKey(psk)
        packet_key = key_mgr.get_packet_key(conn_id, seq)
        
        # Create cipher
        cipher = ChaCha20Poly1305(packet_key)
        
        # Additional authenticated data (conn_id + seq)
        aad = conn_id + seq.to_bytes(4, 'big')
        
        # Encrypt
        ciphertext = cipher.encrypt(nonce, plaintext, aad)
        
        return nonce, ciphertext
    
    @staticmethod
    def decrypt_packet(
        nonce: bytes,
        ciphertext: bytes,
        conn_id: bytes,
        seq: int,
        psk: Optional[bytes] = None,
    ) -> Optional[bytes]:
        """
        Decrypt packet data.
        
        Returns: plaintext or None on authentication failure
        """
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography library not installed")
        
        try:
            # Derive packet key
            key_mgr = EncryptionKey(psk)
            packet_key = key_mgr.get_packet_key(conn_id, seq)
            
            # Create cipher
            cipher = ChaCha20Poly1305(packet_key)
            
            # Additional authenticated data
            aad = conn_id + seq.to_bytes(4, 'big')
            
            # Decrypt
            plaintext = cipher.decrypt(nonce, ciphertext, aad)
            
            return plaintext
        
        except Exception:
            # Authentication failed
            return None
    
    def encrypt_stream(
        self,
        data: bytes,
        conn_id: bytes,
        start_seq: int = 0,
    ) -> bytes:
        """Encrypt data stream with multiple packets"""
        encrypted = b""
        
        for i, byte_val in enumerate(data):
            seq = start_seq + i // 256
            
            nonce, ct = self.encrypt_packet(
                bytes([byte_val]),
                conn_id,
                seq,
                self.key.key,
            )
            
            # Frame: nonce(12) + ciphertext
            encrypted += nonce + ct
        
        return encrypted
    
    def decrypt_stream(
        self,
        encrypted: bytes,
        conn_id: bytes,
        start_seq: int = 0,
    ) -> Optional[bytes]:
        """Decrypt data stream"""
        decrypted = b""
        offset = 0
        
        while offset < len(encrypted):
            if offset + 12 + 1 + 16 > len(encrypted):
                break  # Not enough data
            
            nonce = encrypted[offset:offset+12]
            offset += 12
            
            # Ciphertext includes 16-byte tag
            ciphertext = encrypted[offset:offset+17]
            offset += 17
            
            seq = start_seq + len(decrypted) // 256
            
            plaintext = self.decrypt_packet(
                nonce,
                ciphertext,
                conn_id,
                seq,
                self.key.key,
            )
            
            if plaintext is None:
                return None  # Authentication failed
            
            decrypted += plaintext
        
        return decrypted


def test_encryption():
    """Test encryption"""
    if not CRYPTO_AVAILABLE:
        print("⚠️  cryptography library not installed")
        print("   Install with: pip install cryptography")
        return
    
    print("🔐 Testing STORM Encryption\n")
    
    # Create key
    psk = secrets.token_bytes(32)
    print(f"PSK: {psk.hex()[:32]}...")
    
    # Test single packet
    plaintext = b"Hello, STORM encrypted!"
    conn_id = secrets.token_bytes(4)
    seq = 0
    
    print(f"\n📝 Plaintext: {plaintext}")
    print(f"   conn_id: {conn_id.hex()}")
    print(f"   seq: {seq}")
    
    # Encrypt
    nonce, ciphertext = STORMCrypto.encrypt_packet(plaintext, conn_id, seq, psk)
    print(f"\n🔐 Encrypted:")
    print(f"   nonce: {nonce.hex()}")
    print(f"   ciphertext: {ciphertext.hex()}")
    print(f"   size: {len(nonce) + len(ciphertext)} bytes (overhead: 28B)")
    
    # Decrypt
    recovered = STORMCrypto.decrypt_packet(nonce, ciphertext, conn_id, seq, psk)
    print(f"\n🔓 Decrypted: {recovered}")
    
    if recovered == plaintext:
        print("✅ Encryption/decryption successful!")
    else:
        print("❌ Decryption failed!")
    
    # Test authentication failure
    print(f"\n🔒 Testing authentication...")
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    result = STORMCrypto.decrypt_packet(nonce, tampered, conn_id, seq, psk)
    
    if result is None:
        print("✅ Tampered data detected!")
    else:
        print("❌ Failed to detect tampering!")


if __name__ == "__main__":
    test_encryption()
