"""End-to-end integration tests running CLI commands on generated data."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from solution.cli import main
from solution.config import REPO_ROOT


def test_cli_end_to_end_workflow(tmp_path: Path):
    """Execute full user flow: generate data -> ingest drop 1 -> ingest drop 2 -> report -> verify."""
    data_dir = tmp_path / "data"
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    subprocess.check_call([sys.executable, str(gen_script), "--seed", "42", "--scale", "small", "--out", str(data_dir)])

    db_path = tmp_path / "cli_test.db"
    reports_dir = tmp_path / "reports"

    # Ingest drop 1
    ret1 = main(["ingest", str(data_dir / "drop_1"), "--db", str(db_path)])
    assert ret1 == 0

    # Ingest drop 2
    ret2 = main(["ingest", str(data_dir / "drop_2"), "--db", str(db_path)])
    assert ret2 == 0

    # Generate reports
    ret3 = main(["report", "--out", str(reports_dir), "--db", str(db_path)])
    assert ret3 == 0

    # Verify invariants
    ret4 = main(["verify", "--db", str(db_path)])
    assert ret4 == 0

    # Verify all 5 files exist
    assert (reports_dir / "batch_summary.csv").is_file()
    assert (reports_dir / "daily_overview.csv").is_file()
    assert (reports_dir / "lead_report.csv").is_file()
    assert (reports_dir / "rejections.csv").is_file()
    assert (reports_dir / "data_quality.json").is_file()

