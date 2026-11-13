"""Unit tests for pure parsing functions (Milestone 1).

All expected values are derived by hand directly from the specification rules.
"""
from __future__ import annotations

from datetime import datetime, timezone
import pytest

from solution.parsing import (
    IST,
    format_ist_timestamp,
    format_utc_timestamp,
    normalize_header,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_phone,
    parse_string,
    parse_timestamp,
    to_ist_date_str,
)


def test_header_normalization():
    """Verify normalization of headers: lower-case, trim, replace '_' and spaces with single space."""
    assert normalize_header("Customer_Name") == "customer name"
    assert normalize_header(" customer   name ") == "customer name"
    assert normalize_header("CUSTOMER__NAME") == "customer name"
    assert normalize_header("policy_no") == "policy no"
    assert normalize_header("  ORDER___DATE  ") == "order date"
    assert normalize_header("") == ""
    assert normalize_header(None) == ""


def test_phone_valid():
    """Valid Indian mobile numbers in multiple formats should all normalize to +91XXXXXXXXXX."""
    inputs = [
        "9876543210",
        "+91 98765 43210",
        "(+91) 9876543210",
        "919876543210",
        "09876543210",
        "91-98765-43210",
        "+919876543210",
        " 98765 43210 ",
    ]
    for raw in inputs:
        assert parse_phone(raw) == "+919876543210", f"Failed for {raw!r}"

    # Valid edge: 10-digit number starting with 91
    # 9198765432 is 10 digits starting with 9 -> valid mobile number, not 12 digits!
    assert parse_phone("9198765432") == "+919198765432"

    # Other valid starting digits (6, 7, 8)
    assert parse_phone("6123456789") == "+916123456789"
    assert parse_phone("7123456789") == "+917123456789"
    assert parse_phone("8123456789") == "+918123456789"


def test_phone_invalid():
    """Invalid phone numbers must return None."""
    invalid_cases = [
        "5876543210",        # Starts with 5 (not 6, 7, 8, or 9)
        "987654321",         # 9 digits
        "98765432101234",    # 14 digits
        "",                  # Empty string
        "N/A",               # Non-numeric
        "022-2345-6789",     # Landline (after dropping 0, starts with 2)
        "+1 415 555 0134",   # US number
        "+91 1234567890",    # After dropping 91, starts with 1
        "0091 9876543210",   # 13 digits (not 12 with 91, nor 11 with 0)
        None,                # Missing
    ]
    for raw in invalid_cases:
        assert parse_phone(raw) is None, f"Expected None for {raw!r}"


def test_date_valid():
    """Valid date formats should return ISO 'YYYY-MM-DD'."""
    assert parse_date("2026-12-01") == "2026-12-01"
    assert parse_date("01/12/2026") == "2026-12-01"
    assert parse_date("01-12-2026") == "2026-12-01"
    assert parse_date("01-Dec-2026") == "2026-12-01"
    assert parse_date("01-dec-2026") == "2026-12-01"
    assert parse_date("01-DEC-2026") == "2026-12-01"

    # Leap year: 2028 is a leap year, 29 Feb is valid
    assert parse_date("29/02/2028") == "2028-02-29"
    assert parse_date("2028-02-29") == "2028-02-29"
    assert parse_date("29-Feb-2028") == "2028-02-29"


def test_date_invalid():
    """Invalid or unpadded dates must return None."""
    invalid_dates = [
        "31/02/2026",    # Impossible calendar date
        "2026-02-30",    # Impossible calendar date
        "12/31/2026",    # Month 31 (day-first expected)
        "2026/12/01",    # Slanted ISO format (not accepted)
        "01-Foo-2026",   # Invalid month abbreviation
        "29/02/2027",    # 2027 is not a leap year
        "1/12/2026",     # Single-digit day (requires zero-padded 2 digits)
        "01/1/2026",     # Single-digit month
        "26-12-01",      # 2-digit year
        "",              # Empty
        "TBD",           # Non-date text
        None,
    ]
    for raw in invalid_dates:
        assert parse_date(raw) is None, f"Expected None for {raw!r}"


def test_decimal_valid():
    """Valid currency decimals with prefixes and rounding."""
    assert parse_decimal("25301.4") == "25301.40"
    assert parse_decimal("₹25,301") == "25301.00"
    assert parse_decimal("Rs. 25301") == "25301.00"
    assert parse_decimal("rs 1,234.567") == "1234.57"
    assert parse_decimal("INR 0.125") == "0.13"  # ROUND_HALF_UP rounds .125 up to .13
    assert parse_decimal("inr12") == "12.00"
    assert parse_decimal("0") == "0.00"
    assert parse_decimal("₹ 1,00,000.50") == "100000.50"


def test_decimal_invalid():
    """Invalid decimals must return None."""
    invalid_cases = [
        "-5",          # Negative
        "₹ -3",        # Negative with prefix
        "N/A",         # Text
        "Rs.",         # Prefix without digits
        "1e5",         # Scientific notation
        "",            # Empty
        None,
    ]
    for raw in invalid_cases:
        assert parse_decimal(raw) is None, f"Expected None for {raw!r}"


def test_enum():
    """Enum values matched case-insensitively and stored lower-case."""
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert parse_enum("HI", allowed) == "hi"
    assert parse_enum(" ta ", allowed) == "ta"
    assert parse_enum("en", allowed) == "en"

    # Invalid values
    assert parse_enum("Hindi", allowed) is None
    assert parse_enum("english", allowed) is None
    assert parse_enum("", allowed) is None
    assert parse_enum("   ", allowed) is None
    assert parse_enum(None, allowed) is None


def test_string():
    """Trim strings, return None if empty."""
    assert parse_string("  Aarav Sharma  ") == "Aarav Sharma"
    assert parse_string("") is None
    assert parse_string("   ") is None
    assert parse_string(None) is None


def test_timestamps():
    """Timestamp parsing, UTC storage formatting, and IST date derivation."""
    # 1. UTC format with 'Z'
    dt_utc = parse_timestamp("2026-10-05T13:20:21Z")
    assert dt_utc is not None
    assert dt_utc.tzinfo == timezone.utc
    assert dt_utc.year == 2026 and dt_utc.hour == 13 and dt_utc.minute == 20
    assert format_utc_timestamp(dt_utc) == "2026-10-05T13:20:21.000000Z"

    # 2. Offset format (+05:30)
    dt_offset = parse_timestamp("2026-10-05T18:50:21+05:30")
    assert dt_offset is not None
    assert dt_offset == dt_utc  # 18:50:21 IST == 13:20:21 UTC
    assert format_utc_timestamp(dt_offset) == "2026-10-05T13:20:21.000000Z"

    # 3. Naive timestamp means IST (+05:30)
    dt_naive = parse_timestamp("2026-10-05 18:50:21")
    assert dt_naive is not None
    assert dt_naive.tzinfo == IST
    assert dt_naive == dt_utc
    assert format_utc_timestamp(dt_naive) == "2026-10-05T13:20:21.000000Z"

    # 4. Fractional seconds
    dt_frac = parse_timestamp("2026-10-05T13:20:21.123456Z")
    assert dt_frac is not None
    assert dt_frac.microsecond == 123456
    assert format_utc_timestamp(dt_frac) == "2026-10-05T13:20:21.123456Z"

    # 5. Invalid timestamps
    assert parse_timestamp("invalid-date") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None


def test_timestamp_ist_midnight_boundary():
    """Verify that an instant around midnight IST correctly computes IST date vs UTC date."""
    # 18:29:00 UTC on 2026-10-05 is 23:59:00 IST on 2026-10-05 (before midnight)
    dt_before = parse_timestamp("2026-10-05T18:29:00Z")
    assert dt_before is not None
    assert to_ist_date_str(dt_before) == "2026-10-05"
    assert format_ist_timestamp(dt_before) == "2026-10-05T23:59:00+05:30"

    # 18:31:00 UTC on 2026-10-05 is 00:01:00 IST on 2026-10-06 (after midnight)
    dt_after = parse_timestamp("2026-10-05T18:31:00Z")
    assert dt_after is not None
    # UTC date is 2026-10-05, but IST date is 2026-10-06!
    assert to_ist_date_str(dt_after) == "2026-10-06"
    assert format_ist_timestamp(dt_after) == "2026-10-06T00:01:00+05:30"

