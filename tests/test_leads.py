"""Tests for Milestone 2: Lead ingestion, validation, merging, and PII protection."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from solution.db import connect, init_db
from solution.leads import ingest_lead_file
from solution.pii import decrypt_pii, get_fernet
from solution.schema import load_schemas


@pytest.fixture
def test_setup(schemas_dir: Path, tmp_path: Path):
    """Fixture providing schemas, keys, and an initialized SQLite database."""
    schemas = load_schemas(schemas_dir)
    hmac_key = b"test_secret_hmac_key_1234567890"
    fernet_key = Fernet.generate_key()
    fernet = get_fernet(fernet_key)
    db_path = tmp_path / "test_leads.db"
    conn = connect(db_path)
    init_db(conn)
    return {
        "schemas": schemas,
        "hmac_key": hmac_key,
        "fernet_key": fernet_key,
        "fernet": fernet,
        "conn": conn,
        "db_path": db_path,
    }


def test_alias_headers_extra_columns_and_reordering(test_setup, tmp_path: Path):
    """Verify renamed and reordered headers via aliases and ignoring extra columns."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    # Header using aliases and extra 'agent_notes' column
    csv_content = (
        "Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\n"
        'hi,Aarav Sharma,MOT/2026/000001,9876543210,01-Dec-2026,"₹25,000",VIP Client\n'
    )
    f = tmp_path / "batch_001.csv"
    f.write_text(csv_content, encoding="utf-8")

    res = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res.status == "ingested"
    assert res.rows_total == 1
    assert res.rows_rejected == 0
    assert res.duplicates_merged == 0
    assert res.field_warnings == 0

    cur = conn.cursor()
    cur.execute("SELECT external_ref, pii_enc, attrs_json FROM leads WHERE workflow_id = 'motor_renewal'")
    row = cur.fetchone()
    assert row is not None
    assert row["external_ref"] == "MOT/2026/000001"

    # Decrypt PII to verify customer_name
    pii = decrypt_pii(fernet, row["pii_enc"])
    assert pii["customer_name"] == "Aarav Sharma"

    # Verify attributes
    attrs = json.loads(row["attrs_json"])
    assert attrs["preferred_language"] == "hi"
    assert attrs["policy_expiry"] == "2026-12-01"
    assert attrs["premium_amount"] == "25000.00"
    assert "agent_notes" not in attrs  # Extra column must be ignored


def test_blank_rows_and_row_numbering(test_setup, tmp_path: Path):
    """Blank rows must be skipped and not counted. Row numbers must reflect non-blank rows."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    csv_content = (
        "customer_name,phone,policy_number\n"
        ",,\n"  # blank row 1
        "Aarav Sharma,9876543210,MOT/2026/000001\n"  # data row 1
        "   ,   ,   \n"  # blank row 2
        "\n"  # empty row 3
        "Priya Patel,invalid_phone,MOT/2026/000002\n"  # data row 2 (should be rejected as row 2)
    )
    f = tmp_path / "batch_blank.csv"
    f.write_text(csv_content, encoding="utf-8")

    res = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res.rows_total == 2
    assert res.rows_rejected == 1

    cur = conn.cursor()
    cur.execute("SELECT row_number, reason FROM lead_rejections WHERE batch_id = 'batch_blank'")
    rej = cur.fetchall()
    assert len(rej) == 1
    # Non-blank row 2 is the invalid phone row
    assert rej[0]["row_number"] == 2
    assert rej[0]["reason"] == "invalid_phone"


def test_first_failure_in_schema_order(test_setup, tmp_path: Path):
    """Row validation walks fields in schema order: first failure wins."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    # In motor_renewal.json, schema order is:
    # 1. customer_name (required, pii)
    # 2. phone (required, phone)
    # 3. policy_number (required, reference)
    csv_content = (
        "customer_name,phone,policy_number\n"
        ",5555555555,MOT/1\n"  # missing customer_name AND bad phone -> missing_required:customer_name wins
        "Vivaan Gupta,5555555555,\n"  # bad phone AND missing policy_number -> invalid_phone wins
    )
    f = tmp_path / "batch_order.csv"
    f.write_text(csv_content, encoding="utf-8")

    res = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res.rows_rejected == 2

    cur = conn.cursor()
    cur.execute("SELECT row_number, reason FROM lead_rejections WHERE batch_id = 'batch_order' ORDER BY row_number")
    rejs = cur.fetchall()
    assert rejs[0]["row_number"] == 1
    assert rejs[0]["reason"] == "missing_required:customer_name"
    assert rejs[1]["row_number"] == 2
    assert rejs[1]["reason"] == "invalid_phone"


def test_optional_invalid_warnings_and_no_warning_on_rejection(test_setup, tmp_path: Path):
    """Optional invalid fields produce null + 1 warning. Rejected rows discard warnings."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    csv_content = (
        "customer_name,phone,policy_number,preferred_language,premium_amount\n"
        "Aarav Sharma,9876543210,MOT/1,Hindi,N/A\n"  # 2 warnings, valid row
        ",9876543211,MOT/2,Hindi,N/A\n"  # rejected row (missing customer_name): warnings must NOT be counted
    )
    f = tmp_path / "batch_warn.csv"
    f.write_text(csv_content, encoding="utf-8")

    res = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res.rows_total == 2
    assert res.rows_rejected == 1
    assert res.field_warnings == 2  # Only the surviving row's 2 warnings


def test_merge_rules_and_defaults(test_setup, tmp_path: Path):
    """First valid row sets batch_id. Later duplicates merge non-null, apply defaults."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    # File 1: initial lead creation
    csv1 = (
        "customer_name,phone,policy_number,premium_amount\n"
        "Aarav Sharma,9876543210,MOT/ORIGINAL,10000.00\n"
    )
    f1 = tmp_path / "batch_001.csv"
    f1.write_text(csv1, encoding="utf-8")
    ingest_lead_file(conn, f1, "motor_renewal", schema, hmac_key, fernet)

    cur = conn.cursor()
    cur.execute("SELECT batch_id, external_ref, pii_enc, attrs_json FROM leads WHERE workflow_id = 'motor_renewal'")
    lead1 = cur.fetchone()
    assert lead1["batch_id"] == "batch_001"
    assert lead1["external_ref"] == "MOT/ORIGINAL"
    attrs1 = json.loads(lead1["attrs_json"])
    assert attrs1["preferred_language"] == "en"  # Default applied
    assert attrs1["premium_amount"] == "10000.00"

    # File 2: duplicate row in batch_002
    # Overwrites policy_number, overwrites premium_amount, customer_name updated, language empty (null, so doesn't overwrite)
    csv2 = (
        "customer_name,phone,policy_number,premium_amount,preferred_language\n"
        "Aarav S.,9876543210,MOT/UPDATED,12500.00,\n"
    )
    f2 = tmp_path / "batch_002.csv"
    f2.write_text(csv2, encoding="utf-8")
    res2 = ingest_lead_file(conn, f2, "motor_renewal", schema, hmac_key, fernet)

    assert res2.duplicates_merged == 1

    cur.execute("SELECT batch_id, external_ref, pii_enc, attrs_json FROM leads WHERE workflow_id = 'motor_renewal'")
    lead2 = cur.fetchone()
    # Permanent batch_id remains batch_001!
    assert lead2["batch_id"] == "batch_001"
    assert lead2["external_ref"] == "MOT/UPDATED"
    pii2 = decrypt_pii(fernet, lead2["pii_enc"])
    assert pii2["customer_name"] == "Aarav S."
    attrs2 = json.loads(lead2["attrs_json"])
    assert attrs2["premium_amount"] == "12500.00"
    assert attrs2["preferred_language"] == "en"  # Kept original language


def test_same_phone_in_two_workflows_are_distinct(test_setup, tmp_path: Path):
    """The same phone number uploaded to two different workflows creates two distinct leads."""
    conn = test_setup["conn"]
    schemas = test_setup["schemas"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    phone = "9876543210"

    csv_motor = f"customer_name,phone,policy_number\nAarav, {phone}, MOT/1\n"
    f_motor = tmp_path / "batch_motor.csv"
    f_motor.write_text(csv_motor, encoding="utf-8")
    ingest_lead_file(conn, f_motor, "motor_renewal", schemas["motor_renewal"], hmac_key, fernet)

    csv_fb = f"customer_name,phone,order_id\nAarav, {phone}, ORD/1\n"
    f_fb = tmp_path / "batch_fb.csv"
    f_fb.write_text(csv_fb, encoding="utf-8")
    ingest_lead_file(conn, f_fb, "policy_feedback", schemas["policy_feedback"], hmac_key, fernet)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM leads")
    assert cur.fetchone()[0] == 2

    cur.execute("SELECT workflow_id, phone_hash, phone_last4 FROM leads ORDER BY workflow_id")
    rows = cur.fetchall()
    assert rows[0]["workflow_id"] == "motor_renewal"
    assert rows[1]["workflow_id"] == "policy_feedback"
    # Keyed HMAC hashes must be different because workflow_id is included in the hash
    assert rows[0]["phone_hash"] != rows[1]["phone_hash"]
    assert rows[0]["phone_last4"] == "3210"
    assert rows[1]["phone_last4"] == "3210"


def test_pii_never_appears_in_database_plaintext(test_setup, tmp_path: Path):
    """Raw database file bytes must contain no raw phone digits or customer names."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]
    db_path = test_setup["db_path"]

    raw_phone = "9876543210"
    name = "SecretCustomerUniqueNameXYZ"
    csv_content = f"customer_name,phone,policy_number\n{name},{raw_phone},MOT/SECRET\n"
    f = tmp_path / "batch_sec.csv"
    f.write_text(csv_content, encoding="utf-8")

    ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    conn.commit()
    conn.close()

    # Read binary contents of the SQLite DB file
    db_bytes = db_path.read_bytes()
    assert raw_phone.encode("utf-8") not in db_bytes
    assert name.encode("utf-8") not in db_bytes


def test_reingesting_same_file_is_idempotent(test_setup, tmp_path: Path):
    """Re-ingesting an already ingested file returns 'skipped' and leaves DB unchanged."""
    conn = test_setup["conn"]
    schema = test_setup["schemas"]["motor_renewal"]
    hmac_key = test_setup["hmac_key"]
    fernet = test_setup["fernet"]

    csv_content = "customer_name,phone,policy_number\nAarav,9876543210,MOT/1\n"
    f = tmp_path / "batch_idem.csv"
    f.write_text(csv_content, encoding="utf-8")

    res1 = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res1.status == "ingested"

    res2 = ingest_lead_file(conn, f, "motor_renewal", schema, hmac_key, fernet)
    assert res2.status == "skipped"

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ingested_files")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM leads")
    assert cur.fetchone()[0] == 1
