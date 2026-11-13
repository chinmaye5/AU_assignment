"""Pure business domain logic: event linking, calls, effective disposition, and lead buckets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from solution.parsing import IST, format_ist_timestamp, parse_timestamp, to_ist_date_str
from solution.schema import DispositionCatalog


@dataclass(frozen=True)
class LeadRecord:
    """A customer lead record loaded from storage."""
    workflow_id: str
    phone_hash: str
    phone_last4: str
    batch_id: str
    external_ref: str


@dataclass(frozen=True)
class EventRecord:
    """An event loaded from the append-only ledger."""
    event_id: str
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at_utc: str | None
    phone_hash: str | None
    disposition_code: str | None
    line_no: int = 0


@dataclass(frozen=True)
class CallRecord:
    """A derived call attempt initiated on a specific IST date."""
    workflow_id: str
    call_id: str
    initiated_at_utc: str
    initiated_dt: datetime
    date_ist: str
    connected: bool
    telephony_failed: bool
    effective_disposition: str | None
    is_terminal: bool
    is_success: bool


@dataclass(frozen=True)
class LinkingResult:
    """Result of linking ledger events against current leads."""
    linked_events_by_lead: dict[tuple[str, str], list[EventRecord]]
    linked_events_by_call: dict[tuple[str, str], list[EventRecord]]
    orphan_count: int
    unknown_disposition_count: int


def link_events(
    events: list[EventRecord],
    leads: list[LeadRecord],
    dispositions: DispositionCatalog,
    valid_workflow_ids: set[str],
) -> LinkingResult:
    """Link events to leads dynamically at report time (Section 4.4).
    
    1. Events with phone link to lead via (workflow_id, phone_hash).
    2. Overrides (no phone) link via (workflow_id, call_id) to the call's other linked events.
    3. Events with NULL occurred_at, unknown workflow, or unlinked identity are orphans.
    4. Among linked events: disposition events with unknown codes count as unknown_disposition_events.
    """
    known_leads: set[tuple[str, str]] = {(ld.workflow_id, ld.phone_hash) for ld in leads}

    # Step 1: Link non-override events that carry a phone
    call_to_lead: dict[tuple[str, str], tuple[str, str]] = {}
    lead_events: dict[tuple[str, str], list[EventRecord]] = {}
    linked_events_by_call: dict[tuple[str, str], list[EventRecord]] = {}
    overrides: list[EventRecord] = []
    orphan_count = 0

    for ev in events:
        # Unusable timestamp or unknown workflow => orphan
        if not ev.occurred_at_utc or not ev.workflow_id or ev.workflow_id not in valid_workflow_ids:
            orphan_count += 1
            continue

        if ev.event_type == "disposition.override":
            overrides.append(ev)
            continue

        if ev.phone_hash and (ev.workflow_id, ev.phone_hash) in known_leads:
            lead_key = (ev.workflow_id, ev.phone_hash)
            lead_events.setdefault(lead_key, []).append(ev)
            if ev.call_id:
                call_key = (ev.workflow_id, ev.call_id)
                call_to_lead[call_key] = lead_key
                linked_events_by_call.setdefault(call_key, []).append(ev)
        else:
            orphan_count += 1

    # Step 2: Link disposition.override events via call_id
    for ov in overrides:
        if ov.call_id:
            call_key = (ov.workflow_id, ov.call_id)
            if call_key in call_to_lead:
                lead_key = call_to_lead[call_key]
                lead_events.setdefault(lead_key, []).append(ov)
                linked_events_by_call.setdefault(call_key, []).append(ov)
                continue
        orphan_count += 1

    # Step 3: Count unknown disposition events among linked events
    unknown_disposition_count = 0
    for lead_key, ev_list in lead_events.items():
        wf_id = lead_key[0]
        for ev in ev_list:
            if ev.event_type in ("disposition.set", "disposition.override"):
                code = ev.disposition_code
                if not code or not dispositions.is_known(wf_id, code):
                    unknown_disposition_count += 1

    return LinkingResult(
        linked_events_by_lead=lead_events,
        linked_events_by_call=linked_events_by_call,
        orphan_count=orphan_count,
        unknown_disposition_count=unknown_disposition_count,
    )


def compute_effective_disposition(
    workflow_id: str,
    events: list[EventRecord],
    dispositions: DispositionCatalog,
) -> tuple[str | None, bool, bool]:
    """Compute effective disposition for a call (Section 4.5).
    
    1. Latest valid disposition.override by occurred_at (tie-break: larger event_id).
    2. If none, latest valid disposition.set by occurred_at (tie-break: larger event_id).
    3. If none, (None, False, False).
    Returns (code, is_terminal, is_success).
    """
    valid_overrides: list[EventRecord] = []
    valid_sets: list[EventRecord] = []

    for ev in events:
        if ev.event_type == "disposition.override":
            if ev.disposition_code and dispositions.is_known(workflow_id, ev.disposition_code):
                valid_overrides.append(ev)
        elif ev.event_type == "disposition.set":
            if ev.disposition_code and dispositions.is_known(workflow_id, ev.disposition_code):
                valid_sets.append(ev)

    def sort_key(ev: EventRecord) -> tuple[str, str]:
        # UTC strings are fixed width, so lexicographical order = time order
        return (ev.occurred_at_utc or "", ev.event_id)

    chosen: EventRecord | None = None
    if valid_overrides:
        valid_overrides.sort(key=sort_key)
        chosen = valid_overrides[-1]  # Latest by time, larger event_id
    elif valid_sets:
        valid_sets.sort(key=sort_key)
        chosen = valid_sets[-1]

    if chosen and chosen.disposition_code:
        info = dispositions.get(workflow_id, chosen.disposition_code)
        if info:
            return (chosen.disposition_code, info[0], info[1])
    return (None, False, False)


def build_calls_for_lead(
    workflow_id: str,
    events: list[EventRecord],
    dispositions: DispositionCatalog,
) -> list[CallRecord]:
    """Build CallRecords for a lead's linked events (Section 4.5).
    
    A call is counted only if its call.initiated event is present.
    If multiple initiated events exist, use earliest by (occurred_at, event_id).
    """
    # Group events by call_id
    events_by_call: dict[str, list[EventRecord]] = {}
    for ev in events:
        if ev.call_id:
            events_by_call.setdefault(ev.call_id, []).append(ev)

    calls: list[CallRecord] = []

    for call_id, call_evs in events_by_call.items():
        # Find call.initiated events
        initiated_evs = [e for e in call_evs if e.event_type == "call.initiated" and e.occurred_at_utc]
        if not initiated_evs:
            # Calls without an initiated event are not counted
            continue

        # Earliest initiated event wins
        initiated_evs.sort(key=lambda e: (e.occurred_at_utc or "", e.event_id))
        earliest_init = initiated_evs[0]

        dt = parse_timestamp(earliest_init.occurred_at_utc)
        if dt is None:
            continue

        date_ist = to_ist_date_str(dt)
        connected = any(e.event_type == "call.connected" for e in call_evs)
        telephony_failed = any(e.event_type == "call.telephony_failed" for e in call_evs)

        disp_code, is_terminal, is_success = compute_effective_disposition(
            workflow_id, call_evs, dispositions
        )

        calls.append(CallRecord(
            workflow_id=workflow_id,
            call_id=call_id,
            initiated_at_utc=earliest_init.occurred_at_utc,
            initiated_dt=dt,
            date_ist=date_ist,
            connected=connected,
            telephony_failed=telephony_failed,
            effective_disposition=disp_code,
            is_terminal=is_terminal,
            is_success=is_success,
        ))

    # Sort calls by initiated time
    calls.sort(key=lambda c: (c.initiated_at_utc, c.call_id))
    return calls


def compute_lead_bucket(calls: list[CallRecord]) -> str:
    """Determine a lead's lifecycle bucket (Section 4.5).
    
    1. yet_to_reach_out: no calls
    2. completed: any call's effective disposition has is_terminal = true
    3. in_progress_telephony_failed: every call has a telephony-failed event
    4. in_progress_dialed: everything else
    """
    if not calls:
        return "yet_to_reach_out"
    if any(c.is_terminal for c in calls):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"

