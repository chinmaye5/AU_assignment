"""Configuration, environment variables, timezones, and constants."""
from __future__ import annotations

import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

# Repository root (parent directory of 'solution/')
REPO_ROOT = Path(__file__).resolve().parent.parent

# Standard Indian Standard Time (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Default paths
DEFAULT_DB_PATH = Path("./au_ledger.db")
DEFAULT_SCHEMA_DIR = REPO_ROOT / "assignment-a-data" / "schemas"

# Warning flags to prevent repeated stderr warnings in the same process
_WARNED_DEV_KEYS = False


def get_db_path(override: str | Path | None = None) -> Path:
    """Resolve database path from CLI override, AU_DB env var, or default."""
    if override:
        return Path(override)
    env_val = os.environ.get("AU_DB")
    if env_val:
        return Path(env_val)
    return DEFAULT_DB_PATH


def get_schema_dir(override: str | Path | None = None) -> Path:
    """Resolve schema directory from CLI override, AU_SCHEMA_DIR env, or default."""
    if override:
        p = Path(override)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()
    env_val = os.environ.get("AU_SCHEMA_DIR")
    if env_val:
        p = Path(env_val)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()
    return DEFAULT_SCHEMA_DIR.resolve()


def get_hmac_key() -> bytes:
    """Return the HMAC secret key for phone hashing."""
    global _WARNED_DEV_KEYS
    key = os.environ.get("AU_HMAC_KEY")
    if key:
        return key.encode("utf-8")
    if not _WARNED_DEV_KEYS:
        sys.stderr.write(
            "WARNING: AU_HMAC_KEY not set. Using insecure development-only HMAC key.\n"
        )
        _WARNED_DEV_KEYS = True
    return b"dev_insecure_hmac_secret_key_au_2026_do_not_use_in_prod"


def get_fernet_key() -> bytes:
    """Return 32-byte urlsafe base64 encoded Fernet key for PII encryption."""
    global _WARNED_DEV_KEYS
    key = os.environ.get("AU_FERNET_KEY")
    if key:
        return key.encode("utf-8")
    if not _WARNED_DEV_KEYS:
        sys.stderr.write(
            "WARNING: AU_FERNET_KEY not set. Using insecure development-only Fernet key.\n"
        )
        _WARNED_DEV_KEYS = True
    # 32 bytes '01234567890123456789012345678901' base64-encoded
    return b"MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="

