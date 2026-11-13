"""Event JSONL ingestion into the append-only SQLite ledger."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution.db import transaction
from solution.files import sha256_file
from solution.parsing import format_utc_timestamp, parse_phone, parse_timestamp
from solution.pii import hash_phone


@dataclass(frozen=True)
class IngestEventResult:
    """Summary of an event file ingestion."""
    status: str  # 'ingested' | 'skipped'
    file_name: str
    lines_total: int
    malformed_events: int
    duplicate_events: int
    events_stored: int


def ingest_event_file(
    conn: sqlite3.Connection,
    file_path: Path,
    hmac_key: bytes,
    known_event_ids: set[str] | None = None,
) -> IngestEventResult:
    """Ingest one event JSONL file into the append-only events ledger."""
    file_name = file_path.name
    file_sha = sha256_file(file_path)

    cur = conn.cursor()

    # 1. Check idempotency: if already ingested with identical SHA-256, skip
    cur.execute(
        """
        SELECT rows_total, malformed_events, duplicate_events
        FROM ingested_files
        WHERE kind = 'events' AND workflow_id = '' AND file_name = ? AND sha256 = ?
        """,
        (file_name, file_sha),
    )
    existing = cur.fetchone()
    if existing:
        return IngestEventResult(
            status="skipped",
            file_name=file_name,
            lines_total=existing["rows_total"],
            malformed_events=existing["malformed_events"],
            duplicate_events=existing["duplicate_events"],
            events_stored=existing["rows_total"] - existing["malformed_events"] - existing["duplicate_events"],
        )

    # 2. Populate known event IDs set if not provided
    if known_event_ids is None:
        cur.execute("SELECT event_id FROM events")
        known_event_ids = {row[0] for row in cur.fetchall()}

    lines_total = 0
    malformed_events = 0
    duplicate_events = 0
    events_to_insert: list[tuple[Any, ...]] = []

    with open(file_path, "r", encoding="utf-8") as f:
        line_num = 0
        for raw_line in f:
            # Skip blank / whitespace-only lines entirely (not counted in lines_total)
            stripped = raw_line.strip()
            if not stripped:
                continue

            line_num += 1
            lines_total += 1

            # Check 1: Malformed JSON
            try:
                obj = json.loads(stripped)
            except Exception:
                malformed_events += 1
                continue

            # Must be a JSON object (dict)
            if not isinstance(obj, dict):
                malformed_events += 1
                continue

            # Check event_id
            event_id = obj.get("event_id")
            if not event_id or not isinstance(event_id, str) or not event_id.strip():
                malformed_events += 1
                continue
            event_id = event_id.strip()

            # Check 2: Duplicate event_id
            if event_id in known_event_ids:
                duplicate_events += 1
                continue

            known_event_ids.add(event_id)

            # Check 3: Extract fields
            event_type = obj.get("event_type")
            if isinstance(event_type, str):
                event_type = event_type.strip()
            else:
                event_type = None

            workflow_id = obj.get("workflow_id")
            if isinstance(workflow_id, str):
                workflow_id = workflow_id.strip()
            else:
                workflow_id = None

            call_id = obj.get("call_id")
            if isinstance(call_id, str):
                call_id = call_id.strip()
            else:
                call_id = None

            # Parse occurred_at
            raw_ts = obj.get("occurred_at")
            parsed_dt = parse_timestamp(raw_ts) if isinstance(raw_ts, str) else None
            occurred_at_utc = format_utc_timestamp(parsed_dt) if parsed_dt else None

            # Parse phone -> phone_hash (never store raw phone)
            raw_phone = obj.get("phone")
            phone_hash = None
            if isinstance(raw_phone, str) and workflow_id:
                canonical_p = parse_phone(raw_phone)
                if canonical_p:
                    phone_hash = hash_phone(hmac_key, workflow_id, canonical_p)

            # Extract payload and disposition_code
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            disposition_code = payload.get("disposition_code")
            if isinstance(disposition_code, str):
                disposition_code = disposition_code.strip()
            else:
                disposition_code = None

            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

            events_to_insert.append((
                event_id,
                line_num,
                event_type,
                workflow_id,
                call_id,
                occurred_at_utc,
                phone_hash,
                disposition_code,
                payload_json,
            ))

    # Single atomic transaction per file
    with transaction(conn):
        ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cur.execute(
            """
            INSERT INTO ingested_files (
                kind, workflow_id, file_name, batch_id, sha256, ingested_at,
                rows_total, malformed_events, duplicate_events
            ) VALUES ('events', '', ?, '', ?, ?, ?, ?, ?)
            """,
            (file_name, file_sha, ingested_at, lines_total, malformed_events, duplicate_events),
        )
        file_id = cur.lastrowid

        if events_to_insert:
            cur.executemany(
                """
                INSERT INTO events (
                    event_id, file_id, line_no, event_type, workflow_id, call_id,
                    occurred_at_utc, phone_hash, disposition_code, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        eid,
                        file_id,
                        lno,
                        etype,
                        wfid,
                        cid,
                        occ,
                        phash,
                        disp,
                        pjson,
                    )
                    for eid, lno, etype, wfid, cid, occ, phash, disp, pjson in events_to_insert
                ],
            )

    return IngestEventResult(
        status="ingested",
        file_name=file_name,
        lines_total=lines_total,
        malformed_events=malformed_events,
        duplicate_events=duplicate_events,
        events_stored=len(events_to_insert),
    )

