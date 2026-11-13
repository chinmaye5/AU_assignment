"""Command-line interface for the solution package."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
from solution.pii import get_fernet
from solution.schema import load_dispositions, load_schemas


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
            # Cache known event IDs to deduplicate quickly
            cur = conn.cursor()
            cur.execute("SELECT event_id FROM events")
            known_event_ids: set[str] = {row[0] for row in cur.fetchall()}

            for drop_dir in drops:
                # 1. Process all lead files first
                lead_files = discover_lead_files(drop_dir)
                for wf_id, csv_path in lead_files:
                    if wf_id not in schemas:
                        sys.stderr.write(f"Warning: no schema for workflow {wf_id}, skipping {csv_path}\n")
                        continue
                    res = ingest_lead_file(conn, csv_path, wf_id, schemas[wf_id], hmac_key, fernet)
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
            # Will be implemented in M4
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"Report target {out_dir} prepared (database: {db_path})")
            return 0
        elif args.command == "verify":
            # Will be implemented in M5
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            print(f"Verified schema at {db_path}")
            return 0
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

