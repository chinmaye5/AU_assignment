"""Tests for Milestone 4: Report generation and contract conformance."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from solution.db import connect, init_db
from solution.events import ingest_event_file
from solution.leads import ingest_lead_file
from solution.pii import get_fernet
from solution.reports import generate_reports
from solution.schema import load_schemas


def test_report_generation_contracts_and_determinism(schemas_dir: Path, tmp_path: Path):
    """Verify exact output contract formatting, columns, sorting, and byte-for-byte determinism."""
    schemas = load_schemas(schemas_dir)
    hmac_key = b"test_secret_hmac_key_1234567890"
    fernet = get_fernet(Fernet.generate_key())

    db_path = tmp_path / "report_test.db"
    conn = connect(db_path)
    init_db(conn)

    # 1. Ingest Leads
    # Lead 1: MOT/001, phone 9876543210
    # Lead 2: MOT/002, phone 9876543211
    # Rejected row: missing name
    lead_csv = (
        "customer_name,phone,policy_number,preferred_language\n"
        "Aarav Sharma,9876543210,MOT/001,en\n"
        "Priya Patel,9876543211,MOT/002,hi\n"
        ",9876543212,MOT/003,ta\n"  # Rejected row
    )
    f_lead = tmp_path / "batch_001.csv"
    f_lead.write_text(lead_csv, encoding="utf-8")
    ingest_lead_file(conn, f_lead, "motor_renewal", schemas["motor_renewal"], hmac_key, fernet)

    # 2. Ingest Events
    # Call 1 for Lead 1 on 2026-10-01: connected, disposition renewed (success, terminal)
    # Call 2 for Lead 1 on 2026-10-01: connected, disposition renewed (another success call on same day for Lead 1!)
    # Call 3 for Lead 2 on 2026-10-01: telephony failed
    events_jsonl = [
        # Call 1
        '{"event_id": "e01", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"}',
        '{"event_id": "e02", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:01:00Z", "phone": "9876543210"}',
        '{"event_id": "e03", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:02:00Z", "phone": "9876543210", "payload": {"disposition_code": "renewed"}}',
        # Call 2 (same day, same lead -> success_leads must count Lead 1 only once)
        '{"event_id": "e04", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T14:00:00Z", "phone": "9876543210"}',
        '{"event_id": "e05", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T14:01:00Z", "phone": "9876543210"}',
        '{"event_id": "e06", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T14:02:00Z", "phone": "9876543210", "payload": {"disposition_code": "renewed"}}',
        # Call 3 (Lead 2 -> telephony failed)
        '{"event_id": "e07", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c3", "occurred_at": "2026-10-01T15:00:00Z", "phone": "9876543211"}',
        '{"event_id": "e08", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c3", "occurred_at": "2026-10-01T15:00:05Z", "phone": "9876543211"}',
    ]
    f_events = tmp_path / "events_0001.jsonl"
    f_events.write_text("\n".join(events_jsonl), encoding="utf-8")
    ingest_event_file(conn, f_events, hmac_key)

    # 3. Generate Reports into out_1
    out_1 = tmp_path / "reports_run_1"
    generate_reports(conn, schemas_dir, out_1)

    # 4. Verify batch_summary.csv
    batch_file = out_1 / "batch_summary.csv"
    assert batch_file.exists()
    with open(batch_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        row = reader[0]
        assert row["workflow_id"] == "motor_renewal"
        assert row["batch_id"] == "batch_001"
        assert row["total_leads"] == "2"
        assert row["completed"] == "1"  # Lead 1 renewed
        assert row["in_progress_telephony_failed"] == "1"  # Lead 2 failed

    # 5. Verify daily_overview.csv
    daily_file = out_1 / "daily_overview.csv"
    assert daily_file.exists()
    with open(daily_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        row = reader[0]
        assert row["workflow_id"] == "motor_renewal"
        assert row["date"] == "2026-10-01"
        assert row["total_calls"] == "3"
        assert row["connected_calls"] == "2"
        # 2 / 3 = 0.66666... formatted as 0.6667
        assert row["connectivity_rate"] == "0.6667"
        # 2 success calls for 1 distinct lead => success_leads must be 1!
        assert row["success_leads"] == "1"

    # 6. Verify lead_report.csv
    lead_file = out_1 / "lead_report.csv"
    assert lead_file.exists()
    with open(lead_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2
        l1, l2 = reader[0], reader[1]
        assert l1["external_ref"] == "MOT/001"
        assert l1["phone_masked"] == "+91******3210"
        assert l1["bucket"] == "completed"
        assert l1["total_calls"] == "2"
        assert l1["connected_calls"] == "2"
        assert l1["current_disposition"] == "renewed"
        assert l1["ever_success"] == "true"
        # IST offset without microseconds
        assert l1["last_call_at"] == "2026-10-01T19:30:00+05:30"

        assert l2["external_ref"] == "MOT/002"
        assert l2["phone_masked"] == "+91******3211"
        assert l2["bucket"] == "in_progress_telephony_failed"
        assert l2["total_calls"] == "1"
        assert l2["connected_calls"] == "0"
        assert l2["current_disposition"] == ""
        assert l2["ever_success"] == "false"

    # 7. Verify rejections.csv
    rej_file = out_1 / "rejections.csv"
    assert rej_file.exists()
    with open(rej_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["row_number"] == "3"
        assert reader[0]["reason"] == "missing_required:customer_name"

    # 8. Verify data_quality.json
    dq_file = out_1 / "data_quality.json"
    assert dq_file.exists()
    dq = json.loads(dq_file.read_text(encoding="utf-8"))
    expected_keys = [
        "lead_rows_total",
        "lead_rows_rejected",
        "rejections_by_reason",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "orphan_events",
        "unknown_disposition_events",
    ]
    assert list(dq.keys()) == expected_keys
    assert dq["lead_rows_total"] == 3
    assert dq["lead_rows_rejected"] == 1
    assert dq["rejections_by_reason"] == {"missing_required:customer_name": 1}
    assert dq["orphan_events"] == 0
    assert dq["unknown_disposition_events"] == 0

    # 9. Verify Determinism: generate reports into out_2 and diff bytes
    out_2 = tmp_path / "reports_run_2"
    generate_reports(conn, schemas_dir, out_2)

    for filename in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        bytes1 = (out_1 / filename).read_bytes()
        bytes2 = (out_2 / filename).read_bytes()
        assert bytes1 == bytes2, f"Determinism mismatch in {filename}"

    conn.close()

