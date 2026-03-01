"""Fernet encryption/decryption + HMAC search index for PII fields."""

import hashlib
import hmac
import os

from cryptography.fernet import Fernet


def _get_fernet():
    key = os.environ.get("FERNET_KEY", "")
    if not key:
        raise ValueError("FERNET_KEY environment variable is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _get_hmac_key():
    """Get HMAC key from environment variable."""
    hmac_key = os.environ.get("HMAC_KEY", "")
    if not hmac_key:
        raise ValueError("HMAC_KEY environment variable is not set")
    return hmac_key.encode() if isinstance(hmac_key, str) else hmac_key


def encrypt(plaintext):
    """Encrypt a plaintext string. Returns base64-encoded ciphertext."""
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext):
    """Decrypt a ciphertext string. Returns plaintext."""
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def hmac_index(value):
    """Generate HMAC-SHA256 index for searching encrypted fields.

    For duplicate disambiguation, pass 'name_employer' as value.
    """
    if not value:
        return ""
    key = _get_hmac_key()
    normalized = value.strip().lower()
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
