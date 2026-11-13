"""Tests for Milestone 0: schema loading, disposition catalog, DB DDL and triggers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from solution.db import connect, init_db
from solution.files import natural_sort_key, sha256_file
from solution.schema import FieldDef, load_dispositions, load_schemas, load_workflow_schema


def test_load_real_schemas(schemas_dir: Path):
    """Verify that both workflows load successfully and have valid definitions."""
    schemas = load_schemas(schemas_dir)
    assert "motor_renewal" in schemas
    assert "policy_feedback" in schemas

    motor = schemas["motor_renewal"]
    assert motor.workflow_id == "motor_renewal"
    assert motor.reference_field.name == "policy_number"
    assert not motor.reference_field.pii
    assert "policy no" in motor.reference_field.aliases

    feedback = schemas["policy_feedback"]
    assert feedback.workflow_id == "policy_feedback"
    assert feedback.reference_field.name == "order_id"
    assert not feedback.reference_field.pii
    assert "order id" in feedback.reference_field.aliases


def test_schema_must_have_exactly_one_reference_field(tmp_path: Path):
    """Schemas with 0 or multiple reference fields must fail loudly."""
    # Zero reference fields
    schema_no_ref = {
        "workflow_id": "bad_wf_0",
        "fields": [
            {"name": "phone", "type": "phone", "required": True, "pii": True},
            {"name": "name", "type": "string", "required": True},
        ],
    }
    f0 = tmp_path / "bad0.json"
    f0.write_text(json.dumps(schema_no_ref), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one field with reference=true"):
        load_workflow_schema(f0)

    # Two reference fields
    schema_two_ref = {
        "workflow_id": "bad_wf_2",
        "fields": [
            {"name": "ref1", "type": "string", "reference": True},
            {"name": "ref2", "type": "string", "reference": True},
        ],
    }
    f2 = tmp_path / "bad2.json"
    f2.write_text(json.dumps(schema_two_ref), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one field with reference=true"):
        load_workflow_schema(f2)


def test_schema_reference_field_cannot_be_pii(tmp_path: Path):
    """Reference field cannot be marked PII."""
    schema_pii_ref = {
        "workflow_id": "bad_pii",
        "fields": [
            {"name": "ref_pii", "type": "string", "reference": True, "pii": True},
        ],
    }
    f = tmp_path / "bad_pii.json"
    f.write_text(json.dumps(schema_pii_ref), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be marked PII"):
        load_workflow_schema(f)


def test_load_dispositions(schemas_dir: Path):
    """Verify loading and querying of dispositions.csv."""
    catalog = load_dispositions(schemas_dir / "dispositions.csv")

    assert catalog.is_known("motor_renewal", "renewed")
    assert catalog.get("motor_renewal", "renewed") == (True, True)

    assert catalog.is_known("motor_renewal", "callback_requested")
    assert catalog.get("motor_renewal", "callback_requested") == (False, False)

    assert catalog.is_known("motor_renewal", "not_interested")
    assert catalog.get("motor_renewal", "not_interested") == (True, False)

    assert catalog.is_known("policy_feedback", "feedback_captured")
    assert catalog.get("policy_feedback", "feedback_captured") == (True, True)

    assert not catalog.is_known("motor_renewal", "RENEWD")
    assert catalog.get("motor_renewal", "RENEWD") is None
    assert catalog.get("motor_renewal", "unknown_code") is None


def test_db_init_and_append_only_triggers(mem_db: sqlite3.Connection):
    """Verify DDL creation and assert UPDATE / DELETE on events are blocked by triggers."""
    cur = mem_db.cursor()
    # Check tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert {"ingested_files", "leads", "lead_rejections", "events"}.issubset(tables)

    # Insert a dummy ingested_file
    cur.execute(
        """
        INSERT INTO ingested_files (kind, workflow_id, file_name, batch_id, sha256, ingested_at)
        VALUES ('events', '', 'events_0001.jsonl', '', 'dummy_hash', '2026-11-13T20:00:00Z')
        """
    )
    file_id = cur.lastrowid

    # Insert an event
    cur.execute(
        """
        INSERT INTO events (event_id, file_id, line_no, event_type, workflow_id, call_id, occurred_at_utc, phone_hash, disposition_code, payload_json)
        VALUES ('evt_001', ?, 1, 'call.initiated', 'motor_renewal', 'call_001', '2026-10-01T10:00:00.000000Z', 'hash', NULL, '{}')
        """,
        (file_id,),
    )
    mem_db.commit()

    # Try updating the event -> should fail with trigger message
    with pytest.raises(sqlite3.DatabaseError, match="events is append-only"):
        cur.execute("UPDATE events SET event_type = 'call.connected' WHERE event_id = 'evt_001'")

    # Try deleting the event -> should fail with trigger message
    with pytest.raises(sqlite3.DatabaseError, match="events is append-only"):
        cur.execute("DELETE FROM events WHERE event_id = 'evt_001'")


def test_natural_sort_and_sha256(tmp_path: Path):
    """Verify natural sorting key and SHA-256 calculation."""
    items = ["drop_10", "drop_1", "drop_2", "drop_20", "drop_3"]
    sorted_items = sorted(items, key=natural_sort_key)
    assert sorted_items == ["drop_1", "drop_2", "drop_3", "drop_10", "drop_20"]

    dummy_file = tmp_path / "hello.txt"
    dummy_file.write_bytes(b"hello world")
    # SHA-256 of 'hello world' is b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9
    assert sha256_file(dummy_file) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

