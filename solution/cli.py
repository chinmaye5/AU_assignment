"""Command-line interface for the solution package."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import get_db_path, get_schema_dir
from solution.db import connect, init_db


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
            # Will be implemented in M2/M3
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            print(f"Initialized database at {db_path} for ingestion of {args.path}")
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

