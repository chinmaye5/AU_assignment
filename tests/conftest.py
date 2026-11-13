"""Shared pytest fixtures."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Ensure repository root is on sys.path so 'solution' is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from solution.config import DEFAULT_SCHEMA_DIR, REPO_ROOT
from solution.db import connect, init_db


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def schemas_dir() -> Path:
    return DEFAULT_SCHEMA_DIR


@pytest.fixture
def mem_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite database initialized with schema."""
    conn = connect(":memory:")
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """File-backed SQLite database in a temporary directory initialized with schema."""
    db_file = tmp_path / "test.db"
    conn = connect(db_file)
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()
