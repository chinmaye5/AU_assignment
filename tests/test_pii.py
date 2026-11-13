"""Unit tests for PII protection: HMAC phone hashing and Fernet encryption."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from solution.pii import decrypt_pii, encrypt_pii, get_fernet, hash_phone, mask_phone


def test_hash_phone():
    """Verify phone hashing is 64 hex chars, deterministic, and keyed per workflow."""
    key = b"secret_key_12345"
    h1 = hash_phone(key, "motor_renewal", "+919876543210")
    assert len(h1) == 64
    assert h1 == hash_phone(key, "motor_renewal", "+919876543210")

    # Different workflow produces different hash for identical phone
    h2 = hash_phone(key, "policy_feedback", "+919876543210")
    assert h2 != h1
    assert len(h2) == 64

    # Different key produces different hash
    h3 = hash_phone(b"different_key", "motor_renewal", "+919876543210")
    assert h3 != h1


def test_mask_phone():
    """Verify phone masking matches specification: +91****** + last 4 digits."""
    assert mask_phone("3210") == "+91******3210"
    assert mask_phone("0000") == "+91******0000"


def test_encrypt_decrypt_pii():
    """Verify Fernet encryption and decryption round-trip."""
    key = Fernet.generate_key()
    fernet = get_fernet(key)

    data = {"customer_name": "Aarav Sharma", "address": "Mumbai"}
    ciphertext = encrypt_pii(fernet, data)

    assert isinstance(ciphertext, bytes)
    assert b"Aarav Sharma" not in ciphertext  # Plaintext must not appear

    decrypted = decrypt_pii(fernet, ciphertext)
    assert decrypted == data


def test_decrypt_with_wrong_key_fails():
    """Attempting decryption with a different key must raise InvalidToken."""
    fernet1 = get_fernet(Fernet.generate_key())
    fernet2 = get_fernet(Fernet.generate_key())

    data = {"customer_name": "Priya Patel"}
    ciphertext = encrypt_pii(fernet1, data)

    with pytest.raises(InvalidToken):
        decrypt_pii(fernet2, ciphertext)

