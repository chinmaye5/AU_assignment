"""PII protection: HMAC phone hashing, Fernet encryption, and phone masking."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from cryptography.fernet import Fernet


def hash_phone(key: bytes, workflow_id: str, canonical_phone: str) -> str:
    """Compute keyed HMAC-SHA256 hash of workflow_id and canonical phone number.
    
    Returns 64-character lowercase hex string.
    """
    message = f"{workflow_id}|{canonical_phone}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def mask_phone(phone_last4: str) -> str:
    """Format masked phone number: '+91******' followed by the last 4 digits."""
    return f"+91******{phone_last4}"


def get_fernet(key: bytes) -> Fernet:
    """Create a Fernet instance from a urlsafe base64-encoded 32-byte key."""
    return Fernet(key)


def encrypt_pii(fernet: Fernet, pii_data: dict[str, Any]) -> bytes:
    """Serialize PII dictionary to JSON and encrypt with Fernet."""
    payload = json.dumps(pii_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return fernet.encrypt(payload)


def decrypt_pii(fernet: Fernet, encrypted_bytes: bytes) -> dict[str, Any]:
    """Decrypt Fernet token and parse back to dictionary."""
    decrypted = fernet.decrypt(encrypted_bytes)
    return json.loads(decrypted.decode("utf-8"))

