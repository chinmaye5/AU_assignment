"""Lead CSV ingestion, schema validation, rejection logging, and merging."""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from solution.db import transaction
from solution.files import sha256_file
from solution.parsing import (
    normalize_header,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_phone,
    parse_string,
)
from solution.pii import PhoneHasher, decrypt_pii, encrypt_pii, hash_phone
from solution.schema import FieldDef, WorkflowSchema


@dataclass(frozen=True)
class IngestLeadResult:
    """Summary of a lead file ingestion."""
    status: str  # 'ingested' | 'skipped'
    file_name: str
    workflow_id: str
    rows_total: int
    rows_rejected: int
    duplicates_merged: int
    field_warnings: int


def _map_columns_to_fields(
    header_row: list[str], schema: WorkflowSchema
) -> dict[int, FieldDef]:
    """Map CSV column indices to schema fields based on normalized names and aliases.
    
    If two columns map to the same field, the first column wins.
    """
    # Build lookup: normalized_name -> FieldDef
    lookup: dict[str, FieldDef] = {}
    for f in schema.fields:
        norm_name = normalize_header(f.name)
        if norm_name not in lookup:
            lookup[norm_name] = f
        for alias in f.aliases:
            norm_alias = normalize_header(alias)
            if norm_alias not in lookup:
                lookup[norm_alias] = f

    col_to_field: dict[int, FieldDef] = {}
    seen_fields: set[str] = set()

    for col_idx, raw_header in enumerate(header_row):
        norm_h = normalize_header(raw_header)
        if norm_h in lookup:
            fdef = lookup[norm_h]
            if fdef.name not in seen_fields:
                col_to_field[col_idx] = fdef
                seen_fields.add(fdef.name)

    return col_to_field


def _parse_field_value(field_def: FieldDef, raw_val: str | None) -> Any:
    """Parse raw string value according to field type. Returns None if invalid or missing."""
    if raw_val is None:
        return None
    t = field_def.type
    if t == "string":
        return parse_string(raw_val)
    elif t == "phone":
        return parse_phone(raw_val)
    elif t == "date":
        return parse_date(raw_val)
    elif t == "decimal":
        return parse_decimal(raw_val)
    elif t == "enum":
        return parse_enum(raw_val, field_def.values)
    return raw_val.strip() if raw_val.strip() else None


def ingest_lead_file(
    conn: sqlite3.Connection,
    file_path: Path,
    workflow_id: str,
    schema: WorkflowSchema,
    hmac_key: bytes,
    fernet: Fernet,
    lead_cache: dict[tuple[str, str], list[Any]] | None = None,
    phone_hasher: PhoneHasher | None = None,
) -> IngestLeadResult:
    """Ingest one lead CSV file against its workflow schema idempotently."""
    file_name = file_path.name
    batch_id = file_path.stem
    file_sha = sha256_file(file_path)

    if phone_hasher is None:
        phone_hasher = PhoneHasher(hmac_key)

    # 1. Check idempotency: if already ingested with same sha256, skip
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rows_total, rows_rejected, duplicates_merged, field_warnings
        FROM ingested_files
        WHERE kind = 'leads' AND workflow_id = ? AND file_name = ? AND sha256 = ?
        """,
        (workflow_id, file_name, file_sha),
    )
    existing = cur.fetchone()
    if existing:
        return IngestLeadResult(
            status="skipped",
            file_name=file_name,
            workflow_id=workflow_id,
            rows_total=existing["rows_total"],
            rows_rejected=existing["rows_rejected"],
            duplicates_merged=existing["duplicates_merged"],
            field_warnings=existing["field_warnings"],
        )

    # 2. Read file content
    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header_row = next(reader)
        except StopIteration:
            header_row = []

        col_to_field = _map_columns_to_fields(header_row, schema)

        # Pre-find the phone field in schema
        phone_field_name = next(f.name for f in schema.fields if f.type == "phone")

        rows_total = 0
        rows_rejected = 0
        duplicates_merged = 0
        field_warnings = 0
        rejections: list[tuple[int, str]] = []  # (row_number, reason)

        # We execute in a single transaction
        with transaction(conn):
            ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            cur.execute(
                """
                INSERT INTO ingested_files (
                    kind, workflow_id, file_name, batch_id, sha256, ingested_at,
                    rows_total, rows_rejected, duplicates_merged, field_warnings
                ) VALUES ('leads', ?, ?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (workflow_id, file_name, batch_id, file_sha, ingested_at),
            )
            file_id = cur.lastrowid

            data_row_num = 0
            for raw_row in reader:
                # Blank rows: all empty or whitespace cells -> skipped and NOT counted
                if not raw_row or all(cell.strip() == "" for cell in raw_row):
                    continue

                data_row_num += 1
                rows_total += 1

                # Extract raw cell for each mapped column
                raw_cell_by_field: dict[str, str] = {}
                for col_idx, fdef in col_to_field.items():
                    if col_idx < len(raw_row):
                        raw_cell_by_field[fdef.name] = raw_row[col_idx]

                # Validate fields strictly in schema order
                row_rejected = False
                rejection_reason = ""
                row_warnings = 0
                parsed_values: dict[str, Any] = {}

                for fdef in schema.fields:
                    raw_val = raw_cell_by_field.get(fdef.name)
                    trimmed = raw_val.strip() if raw_val is not None else ""
                    if not trimmed:
                        # Value is missing
                        if fdef.required:
                            row_rejected = True
                            rejection_reason = f"missing_required:{fdef.name}"
                            break
                        else:
                            parsed_values[fdef.name] = None
                    else:
                        # Value is present, parse it
                        parsed = _parse_field_value(fdef, trimmed)
                        if parsed is None:
                            # Present but invalid
                            if fdef.type == "phone":
                                row_rejected = True
                                rejection_reason = "invalid_phone"
                                break
                            elif fdef.required:
                                row_rejected = True
                                rejection_reason = f"invalid_{fdef.name}"
                                break
                            else:
                                parsed_values[fdef.name] = None
                                row_warnings += 1
                        else:
                            parsed_values[fdef.name] = parsed

                if row_rejected:
                    rows_rejected += 1
                    rejections.append((data_row_num, rejection_reason))
                    continue

                # Surviving row: warnings count
                field_warnings += row_warnings

                # Compute lead identity
                canonical_phone = parsed_values[phone_field_name]
                phone_h = phone_hasher.hash(workflow_id, canonical_phone)
                phone_l4 = canonical_phone[-4:]
                ext_ref = parsed_values[schema.reference_field.name]

                # Partition fields into PII vs Attributes
                pii_data: dict[str, Any] = {}
                attrs_data: dict[str, Any] = {}
                for fdef in schema.fields:
                    if fdef.name == schema.reference_field.name:
                        continue
                    if fdef.type == "phone":
                        continue
                    val = parsed_values.get(fdef.name)
                    if fdef.pii:
                        pii_data[fdef.name] = val
                    else:
                        attrs_data[fdef.name] = val

                # Check if lead already exists
                lead_key = (workflow_id, phone_h)
                cached_lead = lead_cache.get(lead_key) if lead_cache is not None else None
                if cached_lead is None and lead_cache is None:
                    cur.execute(
                        """
                        SELECT id, external_ref, pii_enc, attrs_json
                        FROM leads
                        WHERE workflow_id = ? AND phone_hash = ?
                        """,
                        (workflow_id, phone_h),
                    )
                    r = cur.fetchone()
                    if r is not None:
                        cached_lead = [r["id"], r["external_ref"], r["pii_enc"], r["attrs_json"]]

                if cached_lead is None:
                    # New lead: first valid row sets permanent batch_id
                    # Apply defaults after merging to fields still null
                    for fdef in schema.fields:
                        if fdef.default is not None:
                            if fdef.pii and pii_data.get(fdef.name) is None:
                                pii_data[fdef.name] = fdef.default
                            elif not fdef.pii and attrs_data.get(fdef.name) is None:
                                attrs_data[fdef.name] = fdef.default

                    pii_enc = encrypt_pii(fernet, pii_data)
                    attrs_json = json.dumps(attrs_data, ensure_ascii=False, sort_keys=True)

                    cur.execute(
                        """
                        INSERT INTO leads (
                            workflow_id, phone_hash, phone_last4, batch_id,
                            external_ref, pii_enc, attrs_json, first_file_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            phone_h,
                            phone_l4,
                            batch_id,
                            str(ext_ref),
                            pii_enc,
                            attrs_json,
                            file_id,
                        ),
                    )
                    new_id = cur.lastrowid
                    if lead_cache is not None:
                        lead_cache[lead_key] = [new_id, str(ext_ref), pii_enc, attrs_json]
                else:
                    # Duplicate row: merge into existing lead
                    duplicates_merged += 1
                    lead_id, curr_ref, curr_pii_enc, curr_attrs_json = cached_lead
                    curr_pii = decrypt_pii(fernet, curr_pii_enc)
                    curr_attrs = json.loads(curr_attrs_json)

                    # Overwrite only fields whose new value is non-null
                    if ext_ref is not None:
                        curr_ref = str(ext_ref)

                    for k, v in pii_data.items():
                        if v is not None:
                            curr_pii[k] = v

                    for k, v in attrs_data.items():
                        if v is not None:
                            curr_attrs[k] = v

                    # Defaults apply to fields still null
                    for fdef in schema.fields:
                        if fdef.default is not None:
                            if fdef.pii and curr_pii.get(fdef.name) is None:
                                curr_pii[fdef.name] = fdef.default
                            elif not fdef.pii and curr_attrs.get(fdef.name) is None:
                                curr_attrs[fdef.name] = fdef.default

                    new_pii_enc = encrypt_pii(fernet, curr_pii)
                    new_attrs_json = json.dumps(curr_attrs, ensure_ascii=False, sort_keys=True)

                    cur.execute(
                        """
                        UPDATE leads
                        SET external_ref = ?, pii_enc = ?, attrs_json = ?
                        WHERE id = ?
                        """,
                        (curr_ref, new_pii_enc, new_attrs_json, lead_id),
                    )
                    if lead_cache is not None:
                        lead_cache[lead_key] = [lead_id, curr_ref, new_pii_enc, new_attrs_json]

            # Insert all rejections
            if rejections:
                cur.executemany(
                    """
                    INSERT INTO lead_rejections (file_id, workflow_id, batch_id, row_number, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [(file_id, workflow_id, batch_id, r_num, r_reason) for r_num, r_reason in rejections],
                )

            # Update file summary counters
            cur.execute(
                """
                UPDATE ingested_files
                SET rows_total = ?, rows_rejected = ?, duplicates_merged = ?, field_warnings = ?
                WHERE id = ?
                """,
                (rows_total, rows_rejected, duplicates_merged, field_warnings, file_id),
            )

    return IngestLeadResult(
        status="ingested",
        file_name=file_name,
        workflow_id=workflow_id,
        rows_total=rows_total,
        rows_rejected=rows_rejected,
        duplicates_merged=duplicates_merged,
        field_warnings=field_warnings,
    )

