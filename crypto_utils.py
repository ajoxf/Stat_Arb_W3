"""
Encryption utilities for securing API keys at rest.
Uses Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256).
"""

import os
import base64
import hashlib
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None


def _get_fernet_key() -> bytes:
    """
    Derive a Fernet-compatible key from environment config.

    Priority:
    1. ENCRYPTION_KEY env var (base64-urlsafe 32-byte key)
    2. Derived from FLASK_SECRET_KEY via SHA-256
    """
    raw = os.getenv('ENCRYPTION_KEY', '')
    if raw:
        try:
            # Validate it decodes to 32 bytes
            decoded = base64.urlsafe_b64decode(raw + '==')
            if len(decoded) == 32:
                return raw.encode()
        except Exception:
            pass

    # Derive from FLASK_SECRET_KEY
    secret = os.getenv('FLASK_SECRET_KEY', 'crypto-arb-secret-key-change-me')
    derived = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(derived)


def get_cipher() -> Fernet:
    """Get (or lazily initialise) the Fernet cipher."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_fernet_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns empty string if input is empty."""
    if not plaintext:
        return ''
    try:
        return get_cipher().encrypt(plaintext.encode()).decode()
    except Exception as exc:
        logger.error("Encryption error: %s", exc)
        raise


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string. Returns empty string if input is empty."""
    if not ciphertext:
        return ''
    try:
        return get_cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Decryption failed - invalid token (wrong key or corrupted data)")
        return ''
    except Exception as exc:
        logger.error("Decryption error: %s", exc)
        return ''
