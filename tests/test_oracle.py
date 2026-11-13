"""Compare pipeline outputs against the independent oracle across multiple seeds."""
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
from tests.oracle import run_oracle


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42])
def test_oracle_vs_pipeline_across_seeds(schemas_dir: Path, tmp_path: Path, seed: int):
    """Run data generator for a seed, execute both pipeline and oracle, assert byte-for-byte identical output."""
    data_dir = tmp_path / f"data_seed_{seed}"

    # Generate synthetic data
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    cmd = [sys.executable, str(gen_script), "--seed", str(seed), "--scale", "small", "--out", str(data_dir)]
    subprocess.check_call(cmd)

    # 1. Run Pipeline
    db_path = tmp_path / f"pipeline_seed_{seed}.db"
    conn = connect(db_path)
    init_db(conn)

    schemas = load_schemas(schemas_dir)
    hmac_key = b"oracle_test_hmac_key_9876543210"
    fernet = get_fernet(Fernet.generate_key())

    drops = discover_drops(data_dir)
    cur = conn.cursor()
    cur.execute("SELECT event_id FROM events")
    known_event_ids: set[str] = {row[0] for row in cur.fetchall()}

    for drop_dir in drops:
        # Leads first
        for wf_id, csv_path in discover_lead_files(drop_dir):
            if wf_id in schemas:
                ingest_lead_file(conn, csv_path, wf_id, schemas[wf_id], hmac_key, fernet)
        # Events second
        for jsonl_path in discover_event_files(drop_dir):
            ingest_event_file(conn, jsonl_path, hmac_key, known_event_ids)

    out_dir = tmp_path / f"pipeline_reports_{seed}"
    generate_reports(conn, schemas_dir, out_dir)
    conn.close()

    # 2. Run Oracle
    oracle_reports = run_oracle(data_dir, schemas_dir)

    # 3. Compare each of the 5 files byte-for-byte
    for filename, oracle_content in oracle_reports.items():
        pipeline_file = out_dir / filename
        assert pipeline_file.exists(), f"Missing pipeline file: {filename}"
        pipeline_content = pipeline_file.read_text(encoding="utf-8")
        assert pipeline_content == oracle_content, (
            f"Seed {seed} output mismatch in {filename}!\n"
            f"--- Pipeline ---\n{pipeline_content[:500]}\n"
            f"--- Oracle ---\n{oracle_content[:500]}"
        )

