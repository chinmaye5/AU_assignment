"""Tests for Milestone 3: Event JSONL ingestion into the append-only ledger."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from solution.db import connect, init_db
from solution.events import ingest_event_file


@pytest.fixture
def events_setup(tmp_path: Path):
    """Fixture providing initialized database and HMAC key for event ingestion tests."""
    hmac_key = b"test_secret_hmac_key_1234567890"
    db_path = tmp_path / "test_events.db"
    conn = connect(db_path)
    init_db(conn)
    return {
        "conn": conn,
        "db_path": db_path,
        "hmac_key": hmac_key,
    }


def test_malformed_and_blank_lines(events_setup, tmp_path: Path):
    """Verify malformed JSON, arrays, missing event_ids, and blank lines handling."""
    conn = events_setup["conn"]
    hmac_key = events_setup["hmac_key"]

    lines = [
        "",  # blank 1 -> not counted
        '{"event_id": "evt_valid", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"}',
        "   ",  # blank 2 -> not counted
        '{"corrupted_json: missing_brace',  # malformed 1: invalid json
        '[{"event_id": "evt_in_array"}]',  # malformed 2: JSON array instead of object
        '{"event_type": "call.ended"}',  # malformed 3: missing event_id
        '{"event_id": "", "event_type": "call.ended"}',  # malformed 4: empty event_id
        '{"event_id": "   ", "event_type": "call.ended"}',  # malformed 5: whitespace event_id
        '{"event_id": null, "event_type": "call.ended"}',  # malformed 6: null event_id
    ]
    f = tmp_path / "events_malformed.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")

    res = ingest_event_file(conn, f, hmac_key)
    assert res.status == "ingested"
    assert res.lines_total == 7  # 1 valid + 6 malformed (2 blanks skipped entirely)
    assert res.malformed_events == 6
    assert res.duplicate_events == 0
    assert res.events_stored == 1

    cur = conn.cursor()
    cur.execute("SELECT event_id FROM events")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt_valid"


def test_event_deduplication_within_file_and_across_files(events_setup, tmp_path: Path):
    """Duplicate event_id within the same file or across files must be dropped."""
    conn = events_setup["conn"]
    hmac_key = events_setup["hmac_key"]

    file1_lines = [
        '{"event_id": "evt_001", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z"}',
        '{"event_id": "evt_002", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:01:00Z"}',
        '{"event_id": "evt_001", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z"}',  # duplicate in file 1
    ]
    f1 = tmp_path / "events_0001.jsonl"
    f1.write_text("\n".join(file1_lines), encoding="utf-8")

    res1 = ingest_event_file(conn, f1, hmac_key)
    assert res1.lines_total == 3
    assert res1.duplicate_events == 1
    assert res1.events_stored == 2

    # File 2 re-uses evt_002 from file 1
    file2_lines = [
        '{"event_id": "evt_002", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:01:00Z"}',  # cross-file duplicate
        '{"event_id": "evt_003", "event_type": "call.ended", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:02:00Z"}',
    ]
    f2 = tmp_path / "events_0002.jsonl"
    f2.write_text("\n".join(file2_lines), encoding="utf-8")

    res2 = ingest_event_file(conn, f2, hmac_key)
    assert res2.lines_total == 2
    assert res2.duplicate_events == 1
    assert res2.events_stored == 1

    cur = conn.cursor()
    cur.execute("SELECT event_id FROM events ORDER BY event_id")
    eids = [r["event_id"] for r in cur.fetchall()]
    assert eids == ["evt_001", "evt_002", "evt_003"]


def test_timestamps_stored_as_fixed_width_utc(events_setup, tmp_path: Path):
    """UTC, offset, and naive timestamps must be converted to fixed-width UTC."""
    conn = events_setup["conn"]
    hmac_key = events_setup["hmac_key"]

    lines = [
        # 1. UTC Z
        '{"event_id": "evt_utc", "workflow_id": "motor_renewal", "occurred_at": "2026-10-05T13:20:21Z"}',
        # 2. Offset +05:30 (18:50:21 IST == 13:20:21 UTC)
        '{"event_id": "evt_offset", "workflow_id": "motor_renewal", "occurred_at": "2026-10-05T18:50:21+05:30"}',
        # 3. Naive (means IST -> 13:20:21 UTC)
        '{"event_id": "evt_naive", "workflow_id": "motor_renewal", "occurred_at": "2026-10-05 18:50:21"}',
        # 4. Unparseable -> stored as NULL
        '{"event_id": "evt_unparseable", "workflow_id": "motor_renewal", "occurred_at": "not-a-timestamp"}',
    ]
    f = tmp_path / "events_ts.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")

    ingest_event_file(conn, f, hmac_key)

    cur = conn.cursor()
    cur.execute("SELECT event_id, occurred_at_utc FROM events ORDER BY event_id")
    rows = {r["event_id"]: r["occurred_at_utc"] for r in cur.fetchall()}

    assert rows["evt_utc"] == "2026-10-05T13:20:21.000000Z"
    assert rows["evt_offset"] == "2026-10-05T13:20:21.000000Z"
    assert rows["evt_naive"] == "2026-10-05T13:20:21.000000Z"
    assert rows["evt_unparseable"] is None


def test_phone_hashing_and_never_store_raw_phone(events_setup, tmp_path: Path):
    """Verify phone is hashed with HMAC, invalid phone produces NULL hash, and raw phone is not stored in DB."""
    conn = events_setup["conn"]
    hmac_key = events_setup["hmac_key"]
    db_path = events_setup["db_path"]

    raw_phone = "9876543210"
    lines = [
        f'{{"event_id": "e_valid_p", "workflow_id": "motor_renewal", "phone": "{raw_phone}", "payload": {{"reason": "ok"}}}}',
        '{"event_id": "e_invalid_p", "workflow_id": "motor_renewal", "phone": "5551234", "payload": {}}',
        '{"event_id": "e_no_p", "workflow_id": "motor_renewal", "payload": {}}',
    ]
    f = tmp_path / "events_phone.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")

    ingest_event_file(conn, f, hmac_key)

    cur = conn.cursor()
    cur.execute("SELECT event_id, phone_hash, payload_json FROM events ORDER BY event_id")
    rows = {r["event_id"]: (r["phone_hash"], r["payload_json"]) for r in cur.fetchall()}

    # Valid phone has 64-char hash
    assert rows["e_valid_p"][0] is not None
    assert len(rows["e_valid_p"][0]) == 64
    assert json.loads(rows["e_valid_p"][1]) == {"reason": "ok"}

    # Invalid phone -> NULL phone_hash
    assert rows["e_invalid_p"][0] is None

    # Missing phone -> NULL phone_hash
    assert rows["e_no_p"][0] is None

    # Check raw DB bytes to verify raw_phone never appears anywhere
    conn.commit()
    conn.close()
    db_bytes = db_path.read_bytes()
    assert raw_phone.encode("utf-8") not in db_bytes


def test_reingest_events_file_is_idempotent(events_setup, tmp_path: Path):
    """Re-ingesting an event file with identical SHA-256 returns skipped."""
    conn = events_setup["conn"]
    hmac_key = events_setup["hmac_key"]

    lines = [
        '{"event_id": "evt_101", "event_type": "call.initiated", "workflow_id": "motor_renewal"}',
    ]
    f = tmp_path / "events_idem.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")

    res1 = ingest_event_file(conn, f, hmac_key)
    assert res1.status == "ingested"
    assert res1.events_stored == 1

    res2 = ingest_event_file(conn, f, hmac_key)
    assert res2.status == "skipped"

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ingested_files")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM events")
    assert cur.fetchone()[0] == 1

