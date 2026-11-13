"""Report generation: loads data from DB, runs domain logic, and writes 5 report files."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from solution.domain import (
    CallRecord,
    EventRecord,
    LeadRecord,
    build_calls_for_lead,
    compute_lead_bucket,
    link_events,
)
from solution.parsing import format_ist_timestamp
from solution.schema import DispositionCatalog, load_dispositions, load_schemas


def generate_reports(
    conn: sqlite3.Connection,
    schema_dir: Path,
    out_dir: Path,
) -> None:
    """Read data from SQLite, run pure domain calculations, and write the 5 output files."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load schemas and dispositions
    schemas = load_schemas(schema_dir)
    dispositions = load_dispositions(schema_dir / "dispositions.csv")
    valid_wf_ids = set(schemas.keys())

    cur = conn.cursor()

    # 2. Load all leads
    cur.execute(
        """
        SELECT workflow_id, phone_hash, phone_last4, batch_id, external_ref
        FROM leads
        ORDER BY workflow_id, external_ref
        """
    )
    leads = [
        LeadRecord(
            workflow_id=r["workflow_id"],
            phone_hash=r["phone_hash"],
            phone_last4=r["phone_last4"],
            batch_id=r["batch_id"],
            external_ref=r["external_ref"],
        )
        for r in cur.fetchall()
    ]

    # 3. Load all events from ledger
    cur.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id, occurred_at_utc, phone_hash, disposition_code, line_no
        FROM events
        ORDER BY occurred_at_utc, event_id
        """
    )
    events = [
        EventRecord(
            event_id=r["event_id"],
            event_type=r["event_type"],
            workflow_id=r["workflow_id"],
            call_id=r["call_id"],
            occurred_at_utc=r["occurred_at_utc"],
            phone_hash=r["phone_hash"],
            disposition_code=r["disposition_code"],
            line_no=r["line_no"],
        )
        for r in cur.fetchall()
    ]

    # 4. Run dynamic linking at report time
    linking_res = link_events(events, leads, dispositions, valid_wf_ids)

    # 5. Process calls and buckets per lead
    lead_calls_map: dict[tuple[str, str], list[CallRecord]] = {}
    lead_bucket_map: dict[tuple[str, str], str] = {}

    for lead in leads:
        lead_key = (lead.workflow_id, lead.phone_hash)
        ev_list = linking_res.linked_events_by_lead.get(lead_key, [])
        calls = build_calls_for_lead(lead.workflow_id, ev_list, dispositions)
        lead_calls_map[lead_key] = calls
        lead_bucket_map[lead_key] = compute_lead_bucket(calls)

    # -------------------------------------------------------------
    # Report 1: batch_summary.csv
    # -------------------------------------------------------------
    # Group leads by (workflow_id, batch_id)
    batch_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {
        "total_leads": 0,
        "yet_to_reach_out": 0,
        "in_progress_telephony_failed": 0,
        "in_progress_dialed": 0,
        "completed": 0,
    })

    for lead in leads:
        b_key = (lead.workflow_id, lead.batch_id)
        bucket = lead_bucket_map[(lead.workflow_id, lead.phone_hash)]
        batch_counts[b_key]["total_leads"] += 1
        batch_counts[b_key][bucket] += 1

    batch_summary_rows = []
    for (wf_id, b_id) in sorted(batch_counts.keys()):
        counts = batch_counts[(wf_id, b_id)]
        batch_summary_rows.append({
            "workflow_id": wf_id,
            "batch_id": b_id,
            "total_leads": counts["total_leads"],
            "yet_to_reach_out": counts["yet_to_reach_out"],
            "in_progress_telephony_failed": counts["in_progress_telephony_failed"],
            "in_progress_dialed": counts["in_progress_dialed"],
            "completed": counts["completed"],
        })

    with open(out_dir / "batch_summary.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id", "batch_id", "total_leads",
                "yet_to_reach_out", "in_progress_telephony_failed",
                "in_progress_dialed", "completed"
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(batch_summary_rows)

    # -------------------------------------------------------------
    # Report 2: daily_overview.csv
    # -------------------------------------------------------------
    # Group calls by (workflow_id, date_ist)
    daily_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "total_calls": 0,
        "connected_calls": 0,
        "success_lead_ids": set(),
    })

    for lead in leads:
        lead_key = (lead.workflow_id, lead.phone_hash)
        for call in lead_calls_map[lead_key]:
            d_key = (call.workflow_id, call.date_ist)
            daily_stats[d_key]["total_calls"] += 1
            if call.connected:
                daily_stats[d_key]["connected_calls"] += 1
            if call.is_success:
                # distinct leads with at least one success call on that date
                daily_stats[d_key]["success_lead_ids"].add(lead.phone_hash)

    daily_overview_rows = []
    for (wf_id, d_str) in sorted(daily_stats.keys()):
        stats = daily_stats[(wf_id, d_str)]
        tot = stats["total_calls"]
        conn_cnt = stats["connected_calls"]
        rate = (conn_cnt / tot) if tot > 0 else 0.0
        succ_leads = len(stats["success_lead_ids"])

        daily_overview_rows.append({
            "workflow_id": wf_id,
            "date": d_str,
            "total_calls": tot,
            "connected_calls": conn_cnt,
            "connectivity_rate": f"{rate:.4f}",
            "success_leads": succ_leads,
        })

    with open(out_dir / "daily_overview.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id", "date", "total_calls", "connected_calls",
                "connectivity_rate", "success_leads"
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(daily_overview_rows)

    # -------------------------------------------------------------
    # Report 3: lead_report.csv
    # -------------------------------------------------------------
    # One row per lead, sorted by workflow_id, external_ref
    leads_sorted = sorted(leads, key=lambda ld: (ld.workflow_id, ld.external_ref))
    lead_report_rows = []

    for lead in leads_sorted:
        lead_key = (lead.workflow_id, lead.phone_hash)
        calls = lead_calls_map[lead_key]
        bucket = lead_bucket_map[lead_key]

        total_calls = len(calls)
        connected_calls = sum(1 for c in calls if c.connected)

        # last_call_at: latest call.initiated time as ISO-8601 in IST with offset, no microseconds
        if calls:
            # calls are sorted by initiated_at_utc ascending
            latest_call = max(calls, key=lambda c: (c.initiated_at_utc, c.call_id))
            last_call_at = format_ist_timestamp(latest_call.initiated_dt)
        else:
            last_call_at = ""

        # current_disposition: effective disposition of the most recent call (by initiated time) that has one
        calls_with_disp = [c for c in calls if c.effective_disposition]
        if calls_with_disp:
            # tie break: larger call_id
            most_recent_disp_call = max(
                calls_with_disp, key=lambda c: (c.initiated_at_utc, c.call_id)
            )
            current_disposition = most_recent_disp_call.effective_disposition
        else:
            current_disposition = ""

        ever_success = "true" if any(c.is_success for c in calls) else "false"
        phone_masked = f"+91******{lead.phone_last4}"

        lead_report_rows.append({
            "workflow_id": lead.workflow_id,
            "external_ref": lead.external_ref,
            "batch_id": lead.batch_id,
            "phone_masked": phone_masked,
            "bucket": bucket,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": current_disposition,
            "ever_success": ever_success,
        })

    with open(out_dir / "lead_report.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "workflow_id", "external_ref", "batch_id", "phone_masked",
                "bucket", "total_calls", "connected_calls", "last_call_at",
                "current_disposition", "ever_success"
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(lead_report_rows)

    # -------------------------------------------------------------
    # Report 4: rejections.csv
    # -------------------------------------------------------------
    cur.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id, batch_id, row_number
        """
    )
    rejection_rows = [
        {
            "workflow_id": r["workflow_id"],
            "batch_id": r["batch_id"],
            "row_number": r["row_number"],
            "reason": r["reason"],
        }
        for r in cur.fetchall()
    ]

    with open(out_dir / "rejections.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["workflow_id", "batch_id", "row_number", "reason"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rejection_rows)

    # -------------------------------------------------------------
    # Report 5: data_quality.json
    # -------------------------------------------------------------
    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_total ELSE 0 END), 0) AS lead_rows_total,
            COALESCE(SUM(CASE WHEN kind = 'leads' THEN rows_rejected ELSE 0 END), 0) AS lead_rows_rejected,
            COALESCE(SUM(CASE WHEN kind = 'leads' THEN duplicates_merged ELSE 0 END), 0) AS duplicate_lead_rows_merged,
            COALESCE(SUM(CASE WHEN kind = 'leads' THEN field_warnings ELSE 0 END), 0) AS field_warnings,
            COALESCE(SUM(CASE WHEN kind = 'events' THEN rows_total ELSE 0 END), 0) AS events_total,
            COALESCE(SUM(CASE WHEN kind = 'events' THEN malformed_events ELSE 0 END), 0) AS malformed_events,
            COALESCE(SUM(CASE WHEN kind = 'events' THEN duplicate_events ELSE 0 END), 0) AS duplicate_events_dropped
        FROM ingested_files
        """
    )
    file_stats = cur.fetchone()

    cur.execute(
        """
        SELECT reason, COUNT(*) as cnt
        FROM lead_rejections
        GROUP BY reason
        ORDER BY reason
        """
    )
    rejections_by_reason = {r["reason"]: r["cnt"] for r in cur.fetchall()}

    dq_data = {
        "lead_rows_total": file_stats["lead_rows_total"],
        "lead_rows_rejected": file_stats["lead_rows_rejected"],
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": file_stats["duplicate_lead_rows_merged"],
        "field_warnings": file_stats["field_warnings"],
        "events_total": file_stats["events_total"],
        "malformed_events": file_stats["malformed_events"],
        "duplicate_events_dropped": file_stats["duplicate_events_dropped"],
        "orphan_events": linking_res.orphan_count,
        "unknown_disposition_events": linking_res.unknown_disposition_count,
    }

    with open(out_dir / "data_quality.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(dq_data, indent=2, ensure_ascii=False) + "\n")

