"""Unit tests for pure domain logic (Milestone 4).

All scenarios and expected outcomes are derived by hand directly from the specification.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from solution.domain import (
    CallRecord,
    EventRecord,
    LeadRecord,
    build_calls_for_lead,
    compute_effective_disposition,
    compute_lead_bucket,
    link_events,
)
from solution.schema import DispositionCatalog


@pytest.fixture
def test_dispositions() -> DispositionCatalog:
    """Standard disposition catalog with known test codes."""
    return DispositionCatalog(lookup={
        ("motor_renewal", "renewed"): (True, True),
        ("motor_renewal", "callback_requested"): (False, False),
        ("motor_renewal", "not_interested"): (True, False),
        ("motor_renewal", "dnd"): (True, False),
    })


def test_effective_disposition_rules(test_dispositions):
    """Verify override beats set, later override beats earlier, and unknown codes are ignored."""
    wf = "motor_renewal"

    # Case 1: set only
    evs1 = [
        EventRecord("e1", "disposition.set", wf, "c1", "2026-10-01T10:00:00.000000Z", "h", "callback_requested"),
    ]
    code, term, succ = compute_effective_disposition(wf, evs1, test_dispositions)
    assert code == "callback_requested"
    assert not term and not succ

    # Case 2: override beats set
    evs2 = [
        EventRecord("e1", "disposition.set", wf, "c1", "2026-10-01T10:00:00.000000Z", "h", "callback_requested"),
        EventRecord("e2", "disposition.override", wf, "c1", "2026-10-01T12:00:00.000000Z", None, "renewed"),
    ]
    code, term, succ = compute_effective_disposition(wf, evs2, test_dispositions)
    assert code == "renewed"
    assert term and succ

    # Case 3: later override beats earlier override
    evs3 = [
        EventRecord("e1", "disposition.set", wf, "c1", "2026-10-01T10:00:00.000000Z", "h", "callback_requested"),
        EventRecord("e2", "disposition.override", wf, "c1", "2026-10-01T12:00:00.000000Z", None, "renewed"),
        EventRecord("e3", "disposition.override", wf, "c1", "2026-10-01T14:00:00.000000Z", None, "not_interested"),
    ]
    code, term, succ = compute_effective_disposition(wf, evs3, test_dispositions)
    assert code == "not_interested"
    assert term and not succ

    # Case 4: unknown-code override is ignored, falls back to valid set
    evs4 = [
        EventRecord("e1", "disposition.set", wf, "c1", "2026-10-01T10:00:00.000000Z", "h", "callback_requested"),
        EventRecord("e2", "disposition.override", wf, "c1", "2026-10-01T12:00:00.000000Z", None, "UNKNOWN_OVERRIDE_CODE"),
    ]
    code, term, succ = compute_effective_disposition(wf, evs4, test_dispositions)
    assert code == "callback_requested"

    # Case 5: unknown-code set with no override => no disposition (None)
    evs5 = [
        EventRecord("e1", "disposition.set", wf, "c1", "2026-10-01T10:00:00.000000Z", "h", "RENEWD"),
    ]
    code, term, succ = compute_effective_disposition(wf, evs5, test_dispositions)
    assert code is None
    assert not term and not succ


def test_bucket_hierarchy_order():
    """Verify bucket matching order: yet_to_reach_out -> completed -> telephony_failed -> dialed."""
    dt = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)

    # 1. No calls => yet_to_reach_out
    assert compute_lead_bucket([]) == "yet_to_reach_out"

    # 2. Terminal beats telephony-failed
    c_failed = CallRecord(
        workflow_id="wf", call_id="c1", initiated_at_utc="2026-10-01T10:00:00.000000Z",
        initiated_dt=dt, date_ist="2026-10-01", connected=False, telephony_failed=True,
        effective_disposition=None, is_terminal=False, is_success=False,
    )
    c_terminal = CallRecord(
        workflow_id="wf", call_id="c2", initiated_at_utc="2026-10-01T12:00:00.000000Z",
        initiated_dt=dt, date_ist="2026-10-01", connected=True, telephony_failed=False,
        effective_disposition="renewed", is_terminal=True, is_success=True,
    )
    # Both failed and terminal present => completed wins!
    assert compute_lead_bucket([c_failed, c_terminal]) == "completed"

    # 3. All telephony failed with no terminal => in_progress_telephony_failed
    assert compute_lead_bucket([c_failed]) == "in_progress_telephony_failed"
    c_failed2 = CallRecord(
        workflow_id="wf", call_id="c3", initiated_at_utc="2026-10-01T14:00:00.000000Z",
        initiated_dt=dt, date_ist="2026-10-01", connected=False, telephony_failed=True,
        effective_disposition=None, is_terminal=False, is_success=False,
    )
    assert compute_lead_bucket([c_failed, c_failed2]) == "in_progress_telephony_failed"

    # 4. One failed + one dialed-not-failed (e.g. connected or ringing) => in_progress_dialed
    c_dialed = CallRecord(
        workflow_id="wf", call_id="c4", initiated_at_utc="2026-10-01T15:00:00.000000Z",
        initiated_dt=dt, date_ist="2026-10-01", connected=True, telephony_failed=False,
        effective_disposition="callback_requested", is_terminal=False, is_success=False,
    )
    assert compute_lead_bucket([c_failed, c_dialed]) == "in_progress_dialed"


def test_call_without_initiated_is_not_counted(test_dispositions):
    """A call with call.connected but no call.initiated must be ignored."""
    evs = [
        EventRecord("e1", "call.connected", "motor_renewal", "c_no_init", "2026-10-01T10:00:00.000000Z", "h", None),
        EventRecord("e2", "call.ended", "motor_renewal", "c_no_init", "2026-10-01T10:05:00.000000Z", "h", None),
    ]
    calls = build_calls_for_lead("motor_renewal", evs, test_dispositions)
    assert len(calls) == 0


def test_override_linking_and_orphan_handling(test_dispositions):
    """Override links via call_id to lead. Override on unlinked call is orphan."""
    leads = [LeadRecord("motor_renewal", "phone_hash_1", "3210", "batch_001", "REF001")]
    valid_wfs = {"motor_renewal"}

    events = [
        # Call 1 on lead 1
        EventRecord("e1", "call.initiated", "motor_renewal", "call_1", "2026-10-01T10:00:00.000000Z", "phone_hash_1", None),
        # Override for Call 1 (no phone) -> should link!
        EventRecord("e2", "disposition.override", "motor_renewal", "call_1", "2026-10-01T11:00:00.000000Z", None, "renewed"),
        # Override for Call 999 (which has no other linked events) -> must be orphan!
        EventRecord("e3", "disposition.override", "motor_renewal", "call_999", "2026-10-01T12:00:00.000000Z", None, "renewed"),
    ]

    res = link_events(events, leads, test_dispositions, valid_wfs)
    lead_key = ("motor_renewal", "phone_hash_1")
    assert lead_key in res.linked_events_by_lead
    # e1 and e2 are linked to lead 1
    linked_eids = [e.event_id for e in res.linked_events_by_lead[lead_key]]
    assert linked_eids == ["e1", "e2"]
    # e3 was an orphan
    assert res.orphan_count == 1


def test_orphan_then_lead_resolution(test_dispositions):
    """Events arriving before their lead: orphan in drop 1, resolved once lead arrives."""
    valid_wfs = {"motor_renewal"}
    evs = [
        EventRecord("e1", "call.initiated", "motor_renewal", "call_1", "2026-10-01T10:00:00.000000Z", "future_phone_hash", None),
    ]

    # Before lead arrives: orphan
    res_before = link_events(evs, [], test_dispositions, valid_wfs)
    assert res_before.orphan_count == 1
    assert len(res_before.linked_events_by_lead) == 0

    # After lead arrives in drop 2: linked!
    leads = [LeadRecord("motor_renewal", "future_phone_hash", "9999", "batch_003", "REF999")]
    res_after = link_events(evs, leads, test_dispositions, valid_wfs)
    assert res_after.orphan_count == 0
    assert ("motor_renewal", "future_phone_hash") in res_after.linked_events_by_lead

