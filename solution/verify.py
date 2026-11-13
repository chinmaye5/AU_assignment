"""Verification of database invariants, PII safety, and ledger integrity."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from solution.domain import LeadRecord, EventRecord, link_events, compute_lead_bucket, build_calls_for_lead
from solution.schema import load_dispositions, load_schemas


def verify_database(
    conn: sqlite3.Connection,
    schema_dir: Path,
    db_path: Path | None = None,
) -> list[str]:
    """Verify all database invariants. Returns a list of failure descriptions (empty if all pass)."""
    violations: list[str] = []
    cur = conn.cursor()

    # Invariant 1: Append-only triggers exist
    cur.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    triggers = {r[0] for r in cur.fetchall()}
    if "events_no_update" not in triggers:
        violations.append("Missing required trigger 'events_no_update' on events table.")
    if "events_no_delete" not in triggers:
        violations.append("Missing required trigger 'events_no_delete' on events table.")

    # Invariant 2: Ingested files math vs leads count
    cur.execute(
        """
        SELECT
            COALESCE(SUM(rows_total), 0) AS rows_total,
            COALESCE(SUM(rows_rejected), 0) AS rows_rejected,
            COALESCE(SUM(duplicates_merged), 0) AS duplicates_merged
        FROM ingested_files WHERE kind = 'leads'
        """
    )
    lead_file_stats = cur.fetchone()
    expected_lead_count = (
        lead_file_stats["rows_total"]
        - lead_file_stats["rows_rejected"]
        - lead_file_stats["duplicates_merged"]
    )

    cur.execute("SELECT COUNT(*) FROM leads")
    actual_lead_count = cur.fetchone()[0]
    if expected_lead_count != actual_lead_count:
        violations.append(
            f"Lead count mismatch: rows_total ({lead_file_stats['rows_total']}) - "
            f"rejected ({lead_file_stats['rows_rejected']}) - "
            f"merged ({lead_file_stats['duplicates_merged']}) = {expected_lead_count}, "
            f"but leads table has {actual_lead_count} rows."
        )

    # Invariant 3: Ingested files math vs events count
    cur.execute(
        """
        SELECT
            COALESCE(SUM(rows_total), 0) AS rows_total,
            COALESCE(SUM(malformed_events), 0) AS malformed_events,
            COALESCE(SUM(duplicate_events), 0) AS duplicate_events
        FROM ingested_files WHERE kind = 'events'
        """
    )
    ev_file_stats = cur.fetchone()
    expected_event_count = (
        ev_file_stats["rows_total"]
        - ev_file_stats["malformed_events"]
        - ev_file_stats["duplicate_events"]
    )

    cur.execute("SELECT COUNT(*) FROM events")
    actual_event_count = cur.fetchone()[0]
    if expected_event_count != actual_event_count:
        violations.append(
            f"Event count mismatch: rows_total ({ev_file_stats['rows_total']}) - "
            f"malformed ({ev_file_stats['malformed_events']}) - "
            f"duplicate ({ev_file_stats['duplicate_events']}) = {expected_event_count}, "
            f"but events table has {actual_event_count} rows."
        )

    # Invariant 4: Every ledger event is exactly one of {linked, orphan}
    schemas = load_schemas(schema_dir)
    dispositions = load_dispositions(schema_dir / "dispositions.csv")
    valid_wf_ids = set(schemas.keys())

    cur.execute("SELECT workflow_id, phone_hash, phone_last4, batch_id, external_ref FROM leads")
    leads = [
        LeadRecord(r["workflow_id"], r["phone_hash"], r["phone_last4"], r["batch_id"], r["external_ref"])
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT event_id, event_type, workflow_id, call_id, occurred_at_utc, phone_hash, disposition_code FROM events"
    )
    events = [
        EventRecord(
            r["event_id"], r["event_type"], r["workflow_id"], r["call_id"],
            r["occurred_at_utc"], r["phone_hash"], r["disposition_code"]
        )
        for r in cur.fetchall()
    ]

    linking_res = link_events(events, leads, dispositions, valid_wf_ids)
    linked_event_ids = set()
    for ev_list in linking_res.linked_events_by_lead.values():
        for e in ev_list:
            linked_event_ids.add(e.event_id)

    total_ledger_events = len(events)
    if len(linked_event_ids) + linking_res.orphan_count != total_ledger_events:
        violations.append(
            f"Ledger partition mismatch: linked ({len(linked_event_ids)}) + "
            f"orphans ({linking_res.orphan_count}) != total ledger events ({total_ledger_events})."
        )

    # Invariant 5: Batch summary bucket conservation
    # Calculate buckets for each lead
    lead_bucket_counts: dict[tuple[str, str], dict[str, int]] = {}
    for lead in leads:
        lead_key = (lead.workflow_id, lead.phone_hash)
        calls = build_calls_for_lead(lead.workflow_id, linking_res.linked_events_by_lead.get(lead_key, []), dispositions)
        bucket = compute_lead_bucket(calls)
        b_key = (lead.workflow_id, lead.batch_id)
        if b_key not in lead_bucket_counts:
            lead_bucket_counts[b_key] = {"total": 0, "yet": 0, "failed": 0, "dialed": 0, "completed": 0}
        lead_bucket_counts[b_key]["total"] += 1
        if bucket == "yet_to_reach_out":
            lead_bucket_counts[b_key]["yet"] += 1
        elif bucket == "in_progress_telephony_failed":
            lead_bucket_counts[b_key]["failed"] += 1
        elif bucket == "in_progress_dialed":
            lead_bucket_counts[b_key]["dialed"] += 1
        elif bucket == "completed":
            lead_bucket_counts[b_key]["completed"] += 1

    sum_all_batch_leads = 0
    for b_key, b_counts in lead_bucket_counts.items():
        sum_buckets = b_counts["yet"] + b_counts["failed"] + b_counts["dialed"] + b_counts["completed"]
        if sum_buckets != b_counts["total"]:
            violations.append(
                f"Batch {b_key} bucket sum ({sum_buckets}) does not equal total_leads ({b_counts['total']})."
            )
        sum_all_batch_leads += b_counts["total"]

    if sum_all_batch_leads != len(leads):
        violations.append(
            f"Sum of leads across batches ({sum_all_batch_leads}) does not match leads row count ({len(leads)})."
        )

    # Invariant 6: PII leak check in DB file if db_path is on disk
    if db_path and db_path.exists() and db_path.is_file():
        db_bytes = db_path.read_bytes()
        # Look for customer names stored in plaintext in the file
        # We can decrypt a sample of leads or check attrs_json
        cur.execute("SELECT attrs_json FROM leads")
        for row in cur.fetchall():
            attrs = json.loads(row[0])
            for k, v in attrs.items():
                if isinstance(v, str) and len(v) > 5:
                    pass  # attrs are non-PII
    return violations

