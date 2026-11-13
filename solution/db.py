"""Database connection, schema initialization, and transaction management."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingested_files (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('leads','events')),
  workflow_id TEXT NOT NULL DEFAULT '',     -- '' for event files (avoid NULL in UNIQUE)
  file_name TEXT NOT NULL,                  -- e.g. batch_001.csv
  batch_id TEXT NOT NULL DEFAULT '',        -- file stem for lead files
  sha256 TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  rows_total INTEGER NOT NULL DEFAULT 0,        -- lead rows (non-blank) OR event lines (non-empty)
  rows_rejected INTEGER NOT NULL DEFAULT 0,     -- leads only
  duplicates_merged INTEGER NOT NULL DEFAULT 0, -- leads only
  field_warnings INTEGER NOT NULL DEFAULT 0,    -- leads only
  malformed_events INTEGER NOT NULL DEFAULT 0,  -- events only
  duplicate_events INTEGER NOT NULL DEFAULT 0,  -- events only
  UNIQUE (kind, workflow_id, file_name, sha256)
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  phone_hash TEXT NOT NULL CHECK (length(phone_hash) = 64),
  phone_last4 TEXT NOT NULL CHECK (length(phone_last4) = 4),
  batch_id TEXT NOT NULL,
  external_ref TEXT NOT NULL,            -- value of the schema's "reference" field (non-PII)
  pii_enc BLOB NOT NULL,                 -- Fernet-encrypted JSON of all pii fields except phone
  attrs_json TEXT NOT NULL,              -- non-PII, non-reference fields, canonical string values
  first_file_id INTEGER NOT NULL REFERENCES ingested_files(id),
  UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS lead_rejections (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES ingested_files(id),
  workflow_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  row_number INTEGER NOT NULL,
  reason TEXT NOT NULL,
  UNIQUE (file_id, row_number)
);

CREATE TABLE IF NOT EXISTS events (                       -- APPEND-ONLY LEDGER
  event_id TEXT PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES ingested_files(id),
  line_no INTEGER NOT NULL,
  event_type TEXT,
  workflow_id TEXT,
  call_id TEXT,
  occurred_at_utc TEXT,                     -- fixed-width UTC string, NULL if unparseable
  phone_hash TEXT,                          -- NULL if missing/invalid phone
  disposition_code TEXT,                    -- copied from payload for disposition.* events
  payload_json TEXT NOT NULL                -- the payload object only (never the raw phone)
);

CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Connect to SQLite database with foreign keys and WAL mode enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Execute DDL to create tables, indexes, and append-only triggers."""
    conn.executescript(SCHEMA_SQL)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic transaction context manager: commits on exit, rolls back on exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

