"""Command-line interface for the solution package."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from solution.config import (
    get_db_path,
    get_fernet_key,
    get_hmac_key,
    get_schema_dir,
)
from solution.db import connect, init_db
from solution.events import ingest_event_file
from solution.files import (
    discover_drops,
    discover_event_files,
    discover_lead_files,
)
from solution.leads import ingest_lead_file
from solution.pii import PhoneHasher, get_fernet
from solution.reports import generate_reports
from solution.schema import load_dispositions, load_schemas
from solution.verify import verify_database


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="solution",
        description="Assignment A: Interaction Ledger & Reporting Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a drop or parent folder of drops")
    ingest_parser.add_argument("path", type=str, help="Path to drop directory or parent directory")
    ingest_parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    ingest_parser.add_argument("--schemas", type=str, default=None, help="Schema directory path")

    # Report command
    report_parser = subparsers.add_parser("report", help="Generate the 5 report files")
    report_parser.add_argument("--out", type=str, required=True, help="Output directory for reports")
    report_parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    report_parser.add_argument("--schemas", type=str, default=None, help="Schema directory path")

    # Verify command
    verify_parser = subparsers.add_parser("verify", help="Run database invariant checks")
    verify_parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    verify_parser.add_argument("--schemas", type=str, default=None, help="Schema directory path")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = get_db_path(args.db)
    schema_dir = get_schema_dir(args.schemas)

    try:
        if args.command == "ingest":
            schemas = load_schemas(schema_dir)
            hmac_key = get_hmac_key()
            fernet = get_fernet(get_fernet_key())

            conn = connect(db_path)
            init_db(conn)

            drops = discover_drops(Path(args.path))
            cur = conn.cursor()
            # Cache known event IDs and leads to maximize ingestion throughput
            cur.execute("SELECT event_id FROM events")
            known_event_ids: set[str] = {row[0] for row in cur.fetchall()}

            cur.execute("SELECT id, workflow_id, phone_hash, external_ref, pii_enc, attrs_json FROM leads")
            lead_cache: dict[tuple[str, str], list[Any]] = {
                (r[1], r[2]): [r[0], r[3], r[4], r[5]] for r in cur.fetchall()
            }
            phone_hasher = PhoneHasher(hmac_key)

            for drop_dir in drops:
                # 1. Process all lead files first
                lead_files = discover_lead_files(drop_dir)
                for wf_id, csv_path in lead_files:
                    if wf_id not in schemas:
                        sys.stderr.write(f"Warning: no schema for workflow {wf_id}, skipping {csv_path}\n")
                        continue
                    res = ingest_lead_file(
                        conn,
                        csv_path,
                        wf_id,
                        schemas[wf_id],
                        hmac_key,
                        fernet,
                        lead_cache=lead_cache,
                        phone_hasher=phone_hasher,
                    )
                    rel_name = f"{wf_id}/{csv_path.name}"
                    if res.status == "skipped":
                        print(f"leads   {rel_name:<30} skipped (already ingested)")
                    else:
                        print(
                            f"leads   {rel_name:<30} rows={res.rows_total} rejected={res.rows_rejected} "
                            f"merged={res.duplicates_merged} warnings={res.field_warnings}"
                        )

                # 2. Process all event files
                event_files = discover_event_files(drop_dir)
                for jsonl_path in event_files:
                    res_ev = ingest_event_file(conn, jsonl_path, hmac_key, known_event_ids)
                    if res_ev.status == "skipped":
                        print(f"events  {jsonl_path.name:<30} skipped (already ingested)")
                    else:
                        print(
                            f"events  {jsonl_path.name:<30} lines={res_ev.lines_total} "
                            f"malformed={res_ev.malformed_events} duplicates={res_ev.duplicate_events}"
                        )

            conn.close()
            return 0
        elif args.command == "report":
            conn = connect(db_path)
            out_dir = Path(args.out)
            generate_reports(conn, schema_dir, out_dir)
            conn.close()
            print(f"Reports successfully generated in {out_dir}")
            return 0
        elif args.command == "verify":
            conn = connect(db_path)
            violations = verify_database(conn, schema_dir, db_path if db_path.exists() else None)
            conn.close()
            if violations:
                sys.stderr.write("Verification failed with invariants violated:\n")
                for v in violations:
                    sys.stderr.write(f"  - {v}\n")
                return 1
            print(f"All database invariants verified successfully at {db_path}.")
            return 0
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

