"""File discovery, hashing, and ordering helpers."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Calculate the SHA-256 hex digest of a file in streaming chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def natural_sort_key(s: str) -> list[int | str]:
    """Sort key supporting natural numbers so drop_2 comes before drop_10."""
    return [
        int(text) if text.isdigit() else text.lower()
        for text in re.split(r"(\d+)", s)
    ]


def discover_drops(path: Path) -> list[Path]:
    """Discover drop directories from a given path (single drop or parent of drops)."""
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    # If the path directly contains leads/ or events/, it is a single drop
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]

    # Otherwise look for subdirectories named drop_* (or containing leads/events)
    subdirs = [p for p in path.iterdir() if p.is_dir()]
    drop_candidates = [
        p for p in subdirs
        if (p / "leads").is_dir() or (p / "events").is_dir() or p.name.startswith("drop_")
    ]
    # Sort with natural sort key
    return sorted(drop_candidates, key=lambda p: natural_sort_key(p.name))


def discover_lead_files(drop_dir: Path) -> list[tuple[str, Path]]:
    """Discover lead CSV files inside a drop directory.
    
    Returns list of (workflow_id, file_path) sorted by:
    1. workflow_id in alphabetical order
    2. file_path in plain filename order
    """
    leads_dir = drop_dir / "leads"
    if not leads_dir.is_dir():
        return []

    results: list[tuple[str, Path]] = []
    wf_dirs = sorted([p for p in leads_dir.iterdir() if p.is_dir()], key=lambda p: p.name)

    for wf_dir in wf_dirs:
        workflow_id = wf_dir.name
        csv_files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
        for csv_path in csv_files:
            results.append((workflow_id, csv_path))

    return results


def discover_event_files(drop_dir: Path) -> list[Path]:
    """Discover event JSONL files inside a drop directory in plain filename order."""
    events_dir = drop_dir / "events"
    if not events_dir.is_dir():
        return []
    return sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name)

