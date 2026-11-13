"""Independent, simple reference implementation (Oracle) of the specification.

Written strictly from the spec text using Python standard library only.
Does NOT import from `solution/`.
Used to independently verify pipeline correctness on multiple random seeds.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

IST_TZ = timezone(timedelta(hours=5, minutes=30))
MONTHS_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _natural_sort_key(s: str) -> list[int | str]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _normalize_header(h: str | None) -> str:
    if h is None:
        return ""
    return re.sub(r"[_\s]+", " ", h.strip().lower()).strip()


def _parse_phone(val: str | None) -> str | None:
    if not val:
        return None
    d = re.sub(r"\D", "", val)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":
        return f"+91{d}"
    return None


def _parse_date(val: str | None) -> str | None:
    if not val:
        return None
    s = val.strip()
    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    m_slash = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    m_dash = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    m_mon = re.match(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$", s)

    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
    elif m_slash:
        d, m, y = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
    elif m_dash:
        d, m, y = int(m_dash.group(1)), int(m_dash.group(2)), int(m_dash.group(3))
    elif m_mon:
        mon_str = m_mon.group(2).lower()
        if mon_str not in MONTHS_MAP:
            return None
        d, m, y = int(m_mon.group(1)), MONTHS_MAP[mon_str], int(m_mon.group(3))
    else:
        return None

    try:
        return date(y, m, d).isoformat()
    except (ValueError, OverflowError):
        return None


def _parse_decimal(val: str | None) -> str | None:
    if not val:
        return None
    s = val.strip()
    s = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.replace(",", "")
    if not re.match(r"^\d+(\.\d+)?$", s):
        return None
    try:
        d = Decimal(s)
        return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except (InvalidOperation, ValueError):
        return None


def _parse_enum(val: str | None, allowed: list[str]) -> str | None:
    if not val:
        return None
    s = val.strip().lower()
    allowed_set = {a.lower() for a in allowed}
    return s if s in allowed_set else None


def _parse_ts(val: str | None) -> tuple[datetime | None, str | None]:
    if not val or not isinstance(val, str) or not val.strip():
        return (None, None)
    s = val.strip()
    dt = None
    if s.endswith("Z"):
        try:
            dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        except ValueError:
            return (None, None)
    else:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST_TZ)
        except ValueError:
            return (None, None)

    utc_dt = dt.astimezone(timezone.utc)
    return (utc_dt, utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def run_oracle(drops_dir: Path, schema_dir: Path) -> dict[str, str]:
    """Run oracle pipeline on given drop directories and return the 5 report contents."""
    # 1. Load schemas
    schemas: dict[str, dict[str, Any]] = {}
    for sf in sorted(schema_dir.glob("*.json")):
        with open(sf, "r", encoding="utf-8") as f:
            data = json.load(f)
            schemas[data["workflow_id"]] = data

    # Load dispositions
    dispositions: dict[tuple[str, str], tuple[bool, bool]] = {}
    with open(schema_dir / "dispositions.csv", "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            dispositions[(r["workflow_id"].strip(), r["code"].strip())] = (
                r["is_terminal"].strip().lower() == "true",
                r["is_success"].strip().lower() == "true",
            )

    # 2. Discover drops
    if (drops_dir / "leads").is_dir() or (drops_dir / "events").is_dir():
        drop_paths = [drops_dir]
    else:
        subdirs = [p for p in drops_dir.iterdir() if p.is_dir()]
        drop_paths = sorted(
            [p for p in subdirs if (p / "leads").is_dir() or (p / "events").is_dir() or p.name.startswith("drop_")],
            key=lambda p: _natural_sort_key(p.name),
        )

    # Storage structures
    leads: dict[tuple[str, str], dict[str, Any]] = {}  # (wf, phone) -> lead_dict
    rejections: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()

    lead_rows_total = 0
    lead_rows_rejected = 0
    duplicate_leads_merged = 0
    field_warnings_total = 0

    events_total = 0
    malformed_events_total = 0
    duplicate_events_dropped = 0

    # Ingest drops in order
    for drop_path in drop_paths:
        # A. Ingest leads first
        leads_dir = drop_path / "leads"
        if leads_dir.is_dir():
            wf_dirs = sorted([p for p in leads_dir.iterdir() if p.is_dir()], key=lambda p: p.name)
            for wf_dir in wf_dirs:
                wf_id = wf_dir.name
                if wf_id not in schemas:
                    continue
                schema = schemas[wf_id]
                fields = schema["fields"]
                ref_field = next(f for f in fields if f.get("reference"))

                # Pre-build normalized alias map
                norm_to_field: dict[str, dict[str, Any]] = {}
                for f in fields:
                    norm_to_field[_normalize_header(f["name"])] = f
                    for a in f.get("aliases", []):
                        na = _normalize_header(a)
                        if na not in norm_to_field:
                            norm_to_field[na] = f

                csv_files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
                for csv_path in csv_files:
                    batch_id = csv_path.stem
                    with open(csv_path, "r", encoding="utf-8-sig", newline="") as cf:
                        reader = csv.reader(cf)
                        try:
                            header = next(reader)
                        except StopIteration:
                            continue

                        col_map: dict[int, dict[str, Any]] = {}
                        seen_col_fields: set[str] = set()
                        for idx, h in enumerate(header):
                            nh = _normalize_header(h)
                            if nh in norm_to_field:
                                f_obj = norm_to_field[nh]
                                if f_obj["name"] not in seen_col_fields:
                                    col_map[idx] = f_obj
                                    seen_col_fields.add(f_obj["name"])

                        row_num = 0
                        for row in reader:
                            if not row or all(c.strip() == "" for c in row):
                                continue
                            row_num += 1
                            lead_rows_total += 1

                            # Read row
                            raw_by_field: dict[str, str] = {}
                            for idx, f_obj in col_map.items():
                                if idx < len(row):
                                    raw_by_field[f_obj["name"]] = row[idx]

                            rejected = False
                            rej_reason = ""
                            row_warns = 0
                            clean_row: dict[str, Any] = {}

                            for f_obj in fields:
                                fname = f_obj["name"]
                                ftype = f_obj["type"]
                                req = bool(f_obj.get("required"))
                                raw_v = raw_by_field.get(fname, "").strip()

                                if not raw_v:
                                    if req:
                                        rejected = True
                                        rej_reason = f"missing_required:{fname}"
                                        break
                                    clean_row[fname] = None
                                else:
                                    if ftype == "phone":
                                        p = _parse_phone(raw_v)
                                    elif ftype == "date":
                                        p = _parse_date(raw_v)
                                    elif ftype == "decimal":
                                        p = _parse_decimal(raw_v)
                                    elif ftype == "enum":
                                        p = _parse_enum(raw_v, f_obj.get("values", []))
                                    else:
                                        p = raw_v

                                    if p is None:
                                        if ftype == "phone":
                                            rejected = True
                                            rej_reason = "invalid_phone"
                                            break
                                        elif req:
                                            rejected = True
                                            rej_reason = f"invalid_{fname}"
                                            break
                                        else:
                                            clean_row[fname] = None
                                            row_warns += 1
                                    else:
                                        clean_row[fname] = p

                            if rejected:
                                lead_rows_rejected += 1
                                rejections.append({
                                    "workflow_id": wf_id,
                                    "batch_id": batch_id,
                                    "row_number": row_num,
                                    "reason": rej_reason,
                                })
                                continue

                            field_warnings_total += row_warns
                            phone = clean_row["phone"]
                            lead_key = (wf_id, phone)

                            if lead_key not in leads:
                                # Apply defaults
                                for f_obj in fields:
                                    if f_obj.get("default") is not None and clean_row[f_obj["name"]] is None:
                                        clean_row[f_obj["name"]] = f_obj["default"]
                                clean_row["batch_id"] = batch_id
                                clean_row["workflow_id"] = wf_id
                                clean_row["external_ref"] = clean_row[ref_field["name"]]
                                leads[lead_key] = clean_row
                            else:
                                duplicate_leads_merged += 1
                                existing = leads[lead_key]
                                for f_obj in fields:
                                    fname = f_obj["name"]
                                    if clean_row[fname] is not None:
                                        existing[fname] = clean_row[fname]
                                # Defaults for any remaining nulls
                                for f_obj in fields:
                                    fname = f_obj["name"]
                                    if f_obj.get("default") is not None and existing.get(fname) is None:
                                        existing[fname] = f_obj["default"]
                                existing["external_ref"] = existing[ref_field["name"]]

        # B. Ingest events second
        events_dir = drop_path / "events"
        if events_dir.is_dir():
            event_files = sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name)
            for jf in event_files:
                with open(jf, "r", encoding="utf-8") as f:
                    for raw_line in f:
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        events_total += 1
                        try:
                            ev_obj = json.loads(stripped)
                        except Exception:
                            malformed_events_total += 1
                            continue

                        if not isinstance(ev_obj, dict):
                            malformed_events_total += 1
                            continue

                        eid = ev_obj.get("event_id")
                        if not eid or not isinstance(eid, str) or not eid.strip():
                            malformed_events_total += 1
                            continue
                        eid = eid.strip()

                        if eid in seen_event_ids:
                            duplicate_events_dropped += 1
                            continue
                        seen_event_ids.add(eid)

                        dt_utc, ts_utc = _parse_ts(ev_obj.get("occurred_at"))
                        raw_p = ev_obj.get("phone")
                        canonical_p = _parse_phone(raw_p) if isinstance(raw_p, str) else None

                        payload = ev_obj.get("payload") if isinstance(ev_obj.get("payload"), dict) else {}
                        disp_code = payload.get("disposition_code") if isinstance(payload.get("disposition_code"), str) else None

                        events.append({
                            "event_id": eid,
                            "event_type": ev_obj.get("event_type"),
                            "workflow_id": ev_obj.get("workflow_id"),
                            "call_id": ev_obj.get("call_id"),
                            "dt_utc": dt_utc,
                            "occurred_at_utc": ts_utc,
                            "phone": canonical_p,
                            "disposition_code": disp_code.strip() if disp_code else None,
                        })

    # 3. Report time dynamic linking & orphan detection
    valid_wf_ids = set(schemas.keys())
    call_to_lead: dict[tuple[str, str], tuple[str, str]] = {}
    lead_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    overrides: list[dict[str, Any]] = []
    orphan_count = 0

    for ev in events:
        wf = ev["workflow_id"]
        if not ev["occurred_at_utc"] or not wf or wf not in valid_wf_ids:
            orphan_count += 1
            continue

        if ev["event_type"] == "disposition.override":
            overrides.append(ev)
            continue

        if ev["phone"] and (wf, ev["phone"]) in leads:
            l_key = (wf, ev["phone"])
            lead_events[l_key].append(ev)
            if ev["call_id"]:
                call_to_lead[(wf, ev["call_id"])] = l_key
        else:
            orphan_count += 1

    for ov in overrides:
        wf = ov["workflow_id"]
        cid = ov["call_id"]
        if cid and (wf, cid) in call_to_lead:
            l_key = call_to_lead[(wf, cid)]
            lead_events[l_key].append(ov)
        else:
            orphan_count += 1

    unknown_disposition_count = 0
    for (wf, _), ev_list in lead_events.items():
        for ev in ev_list:
            if ev["event_type"] in ("disposition.set", "disposition.override"):
                code = ev["disposition_code"]
                if not code or (wf, code) not in dispositions:
                    unknown_disposition_count += 1

    # 4. Build calls per lead
    lead_calls: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for l_key, ev_list in lead_events.items():
        wf = l_key[0]
        by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in ev_list:
            if e["call_id"]:
                by_call[e["call_id"]].append(e)

        for cid, c_evs in by_call.items():
            inits = [e for e in c_evs if e["event_type"] == "call.initiated" and e["occurred_at_utc"]]
            if not inits:
                continue
            inits.sort(key=lambda e: (e["occurred_at_utc"], e["event_id"]))
            init_ev = inits[0]
            dt = init_ev["dt_utc"].astimezone(IST_TZ)
            date_ist = dt.strftime("%Y-%m-%d")

            connected = any(e["event_type"] == "call.connected" for e in c_evs)
            failed = any(e["event_type"] == "call.telephony_failed" for e in c_evs)

            # Effective disposition
            v_overrides = [
                e for e in c_evs
                if e["event_type"] == "disposition.override" and e["disposition_code"] and (wf, e["disposition_code"]) in dispositions
            ]
            v_sets = [
                e for e in c_evs
                if e["event_type"] == "disposition.set" and e["disposition_code"] and (wf, e["disposition_code"]) in dispositions
            ]
            chosen = None
            if v_overrides:
                v_overrides.sort(key=lambda e: (e["occurred_at_utc"], e["event_id"]))
                chosen = v_overrides[-1]
            elif v_sets:
                v_sets.sort(key=lambda e: (e["occurred_at_utc"], e["event_id"]))
                chosen = v_sets[-1]

            disp_code = chosen["disposition_code"] if chosen else None
            is_term = dispositions[(wf, disp_code)][0] if disp_code else False
            is_succ = dispositions[(wf, disp_code)][1] if disp_code else False

            lead_calls[l_key].append({
                "workflow_id": wf,
                "call_id": cid,
                "occurred_at_utc": init_ev["occurred_at_utc"],
                "dt_ist": dt,
                "date_ist": date_ist,
                "connected": connected,
                "telephony_failed": failed,
                "disposition": disp_code,
                "is_terminal": is_term,
                "is_success": is_succ,
            })
        lead_calls[l_key].sort(key=lambda c: (c["occurred_at_utc"], c["call_id"]))

    # 5. Buckets per lead
    lead_buckets: dict[tuple[str, str], str] = {}
    for l_key in leads:
        c_list = lead_calls[l_key]
        if not c_list:
            lead_buckets[l_key] = "yet_to_reach_out"
        elif any(c["is_terminal"] for c in c_list):
            lead_buckets[l_key] = "completed"
        elif all(c["telephony_failed"] for c in c_list):
            lead_buckets[l_key] = "in_progress_telephony_failed"
        else:
            lead_buckets[l_key] = "in_progress_dialed"

    # Produce outputs
    # 1. batch_summary.csv
    batch_map: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {
        "total": 0, "yet": 0, "failed": 0, "dialed": 0, "completed": 0
    })
    for l_key, l_dict in leads.items():
        b_key = (l_dict["workflow_id"], l_dict["batch_id"])
        b = lead_buckets[l_key]
        batch_map[b_key]["total"] += 1
        if b == "yet_to_reach_out":
            batch_map[b_key]["yet"] += 1
        elif b == "in_progress_telephony_failed":
            batch_map[b_key]["failed"] += 1
        elif b == "in_progress_dialed":
            batch_map[b_key]["dialed"] += 1
        elif b == "completed":
            batch_map[b_key]["completed"] += 1

    bs_buf = io.StringIO()
    bs_writer = csv.DictWriter(
        bs_buf,
        fieldnames=["workflow_id", "batch_id", "total_leads", "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"],
        lineterminator="\n"
    )
    bs_writer.writeheader()
    for (wf, bid) in sorted(batch_map.keys()):
        m = batch_map[(wf, bid)]
        bs_writer.writerow({
            "workflow_id": wf,
            "batch_id": bid,
            "total_leads": m["total"],
            "yet_to_reach_out": m["yet"],
            "in_progress_telephony_failed": m["failed"],
            "in_progress_dialed": m["dialed"],
            "completed": m["completed"],
        })

    # 2. daily_overview.csv
    daily_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "connected": 0, "success_leads": set()
    })
    for l_key, c_list in lead_calls.items():
        for c in c_list:
            d_key = (c["workflow_id"], c["date_ist"])
            daily_stats[d_key]["total"] += 1
            if c["connected"]:
                daily_stats[d_key]["connected"] += 1
            if c["is_success"]:
                daily_stats[d_key]["success_leads"].add(l_key)

    do_buf = io.StringIO()
    do_writer = csv.DictWriter(
        do_buf,
        fieldnames=["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
        lineterminator="\n"
    )
    do_writer.writeheader()
    for (wf, d_str) in sorted(daily_stats.keys()):
        st = daily_stats[(wf, d_str)]
        tot = st["total"]
        conn = st["connected"]
        rate = f"{(conn / tot):.4f}" if tot > 0 else "0.0000"
        do_writer.writerow({
            "workflow_id": wf,
            "date": d_str,
            "total_calls": tot,
            "connected_calls": conn,
            "connectivity_rate": rate,
            "success_leads": len(st["success_leads"]),
        })

    # 3. lead_report.csv
    lr_buf = io.StringIO()
    lr_writer = csv.DictWriter(
        lr_buf,
        fieldnames=["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"],
        lineterminator="\n"
    )
    lr_writer.writeheader()
    sorted_leads = sorted(leads.values(), key=lambda ld: (ld["workflow_id"], ld["external_ref"]))
    for ld in sorted_leads:
        l_key = (ld["workflow_id"], ld["phone"])
        calls = lead_calls[l_key]
        bucket = lead_buckets[l_key]
        tot = len(calls)
        conn = sum(1 for c in calls if c["connected"])
        last_call_at = ""
        if calls:
            latest_call = max(calls, key=lambda c: (c["occurred_at_utc"], c["call_id"]))
            last_call_at = latest_call["dt_ist"].strftime("%Y-%m-%dT%H:%M:%S+05:30")

        calls_with_disp = [c for c in calls if c["disposition"]]
        curr_disp = ""
        if calls_with_disp:
            c_latest_disp = max(calls_with_disp, key=lambda c: (c["occurred_at_utc"], c["call_id"]))
            curr_disp = c_latest_disp["disposition"]

        ever_succ = "true" if any(c["is_success"] for c in calls) else "false"
        p_last4 = ld["phone"][-4:]
        lr_writer.writerow({
            "workflow_id": ld["workflow_id"],
            "external_ref": ld["external_ref"],
            "batch_id": ld["batch_id"],
            "phone_masked": f"+91******{p_last4}",
            "bucket": bucket,
            "total_calls": tot,
            "connected_calls": conn,
            "last_call_at": last_call_at,
            "current_disposition": curr_disp,
            "ever_success": ever_succ,
        })

    # 4. rejections.csv
    rej_buf = io.StringIO()
    rej_writer = csv.DictWriter(
        rej_buf,
        fieldnames=["workflow_id", "batch_id", "row_number", "reason"],
        lineterminator="\n"
    )
    rej_writer.writeheader()
    sorted_rejections = sorted(rejections, key=lambda r: (r["workflow_id"], r["batch_id"], r["row_number"]))
    for r in sorted_rejections:
        rej_writer.writerow(r)

    # 5. data_quality.json
    rejs_by_reason: dict[str, int] = defaultdict(int)
    for r in rejections:
        rejs_by_reason[r["reason"]] += 1

    dq = {
        "lead_rows_total": lead_rows_total,
        "lead_rows_rejected": lead_rows_rejected,
        "rejections_by_reason": dict(sorted(rejs_by_reason.items())),
        "duplicate_lead_rows_merged": duplicate_leads_merged,
        "field_warnings": field_warnings_total,
        "events_total": events_total,
        "malformed_events": malformed_events_total,
        "duplicate_events_dropped": duplicate_events_dropped,
        "orphan_events": orphan_count,
        "unknown_disposition_events": unknown_disposition_count,
    }

    return {
        "batch_summary.csv": bs_buf.getvalue(),
        "daily_overview.csv": do_buf.getvalue(),
        "lead_report.csv": lr_buf.getvalue(),
        "rejections.csv": rej_buf.getvalue(),
        "data_quality.json": json.dumps(dq, indent=2, ensure_ascii=False) + "\n",
    }

