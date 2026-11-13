"""Tests for idempotency, drop order independence, and invariant verification."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from solution.config import REPO_ROOT
from solution.db import connect, init_db
from solution.events import ingest_event_file
from solution.files import discover_drops, discover_event_files, discover_lead_files
from solution.leads import ingest_lead_file
from solution.pii import get_fernet
from solution.reports import generate_reports
from solution.schema import load_schemas
from solution.verify import verify_database


@pytest.fixture(scope="module")
def shared_data_dir(tmp_path_factory) -> Path:
    """Generate small synthetic dataset once for idempotency and order tests."""
    out = tmp_path_factory.mktemp("data_idem")
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    subprocess.check_call([sys.executable, str(gen_script), "--seed", "42", "--scale", "small", "--out", str(out)])
    return out


def test_reingest_idempotency_table_counts_and_reports(schemas_dir: Path, shared_data_dir: Path, tmp_path: Path):
    """Running ingest twice on all drops leaves table counts and report files identical."""
    schemas = load_schemas(schemas_dir)
    hmac_key = b"idem_test_key_1234567890"
    fernet = get_fernet(Fernet.generate_key())

    db_path = tmp_path / "idem.db"
    conn = connect(db_path)
    init_db(conn)

    drops = discover_drops(shared_data_dir)

    def do_ingest_all():
        cur = conn.cursor()
        cur.execute("SELECT event_id FROM events")
        known_ids: set[str] = {r[0] for r in cur.fetchall()}
        for drop_dir in drops:
            for wf_id, csv_path in discover_lead_files(drop_dir):
                if wf_id in schemas:
                    ingest_lead_file(conn, csv_path, wf_id, schemas[wf_id], hmac_key, fernet)
            for jf in discover_event_files(drop_dir):
                ingest_event_file(conn, jf, hmac_key, known_ids)

    # First pass
    do_ingest_all()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM leads")
    leads_count_1 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events")
    events_count_1 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ingested_files")
    files_count_1 = cur.fetchone()[0]

    out_1 = tmp_path / "rep_1"
    generate_reports(conn, schemas_dir, out_1)

    # Second pass: re-run ingest
    do_ingest_all()
    cur.execute("SELECT COUNT(*) FROM leads")
    assert cur.fetchone()[0] == leads_count_1
    cur.execute("SELECT COUNT(*) FROM events")
    assert cur.fetchone()[0] == events_count_1
    cur.execute("SELECT COUNT(*) FROM ingested_files")
    assert cur.fetchone()[0] == files_count_1

    out_2 = tmp_path / "rep_2"
    generate_reports(conn, schemas_dir, out_2)

    # Compare reports
    for fn in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        assert (out_1 / fn).read_bytes() == (out_2 / fn).read_bytes()

    # Invariants verification
    violations = verify_database(conn, schemas_dir, db_path)
    assert not violations, f"Verification violations: {violations}"
    conn.close()


def test_incremental_equivalence_and_events_before_leads(schemas_dir: Path, shared_data_dir: Path, tmp_path: Path):
    """Verify equivalence between:
    DB A: drop_1 then drop_2
    DB B: single ingest of data/
    DB C: events of both drops ingested BEFORE any leads!
    """
    schemas = load_schemas(schemas_dir)
    hmac_key = b"equiv_key_1234567890"
    fernet = get_fernet(Fernet.generate_key())

    drops = discover_drops(shared_data_dir)

    # --- DB A: drop_1 then drop_2 ---
    db_a_path = tmp_path / "db_a.db"
    conn_a = connect(db_a_path)
    init_db(conn_a)
    cur_a = conn_a.cursor()
    known_a: set[str] = set()
    for d in drops:
        for wf_id, p in discover_lead_files(d):
            if wf_id in schemas:
                ingest_lead_file(conn_a, p, wf_id, schemas[wf_id], hmac_key, fernet)
        for p in discover_event_files(d):
            ingest_event_file(conn_a, p, hmac_key, known_a)
    out_a = tmp_path / "rep_a"
    generate_reports(conn_a, schemas_dir, out_a)
    conn_a.close()

    # --- DB B: all drops at once ---
    db_b_path = tmp_path / "db_b.db"
    conn_b = connect(db_b_path)
    init_db(conn_b)
    known_b: set[str] = set()
    for d in drops:
        for wf_id, p in discover_lead_files(d):
            if wf_id in schemas:
                ingest_lead_file(conn_b, p, wf_id, schemas[wf_id], hmac_key, fernet)
        for p in discover_event_files(d):
            ingest_event_file(conn_b, p, hmac_key, known_b)
    out_b = tmp_path / "rep_b"
    generate_reports(conn_b, schemas_dir, out_b)
    conn_b.close()

    # --- DB C: events of both drops ingested BEFORE any leads ---
    db_c_path = tmp_path / "db_c.db"
    conn_c = connect(db_c_path)
    init_db(conn_c)
    known_c: set[str] = set()
    # 1. All events first!
    for d in drops:
        for p in discover_event_files(d):
            ingest_event_file(conn_c, p, hmac_key, known_c)
    # 2. All leads second!
    for d in drops:
        for wf_id, p in discover_lead_files(d):
            if wf_id in schemas:
                ingest_lead_file(conn_c, p, wf_id, schemas[wf_id], hmac_key, fernet)
    out_c = tmp_path / "rep_c"
    generate_reports(conn_c, schemas_dir, out_c)
    conn_c.close()

    # Assert byte-for-byte identity between A, B, and C reports!
    for fn in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        bytes_a = (out_a / fn).read_bytes()
        bytes_b = (out_b / fn).read_bytes()
        bytes_c = (out_c / fn).read_bytes()
        assert bytes_a == bytes_b, f"Mismatch between A and B in {fn}"
        assert bytes_a == bytes_c, f"Mismatch between A and C (events before leads) in {fn}"

