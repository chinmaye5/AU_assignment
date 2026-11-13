"""Pure parsing and normalisation functions for headers, fields, and timestamps."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Indian Standard Time (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Month name mapping for DD-Mon-YYYY (case-insensitive, exactly 3 letters)
MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Regex patterns for strict date parsing
_RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_SLASH_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_RE_DASH_DATE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_RE_MON_DATE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")

# Regex for currency prefixes
_RE_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_RE_DECIMAL_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def normalize_header(header: str | None) -> str:
    """Normalize a header or field name/alias.
    
    Lower-cases, trims, and replaces every run of '_' or whitespace with a single space.
    """
    if header is None:
        return ""
    s = header.strip().lower()
    s = re.sub(r"[_\s]+", " ", s)
    return s.strip()


def parse_string(val: str | None) -> str | None:
    """Trim string. Return None if empty or None."""
    if val is None:
        return None
    s = val.strip()
    return s if s else None


def parse_phone(val: str | None) -> str | None:
    """Parse and normalize Indian mobile phone number.
    
    Rules:
    - Strip all non-digit characters.
    - If 12 digits and starts with '91' -> drop '91'.
    - Else if 11 digits and starts with '0' -> drop '0'.
    - Valid only if exactly 10 digits and starts with 6, 7, 8, or 9.
    - Canonical format: '+91' + 10 digits.
    - Invalid returns None.
    """
    if val is None:
        return None
    digits = re.sub(r"\D", "", val)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(val: str | None) -> str | None:
    """Parse date and return canonical ISO 'YYYY-MM-DD' or None if invalid.
    
    Accepts:
    - YYYY-MM-DD
    - DD/MM/YYYY
    - DD-MM-YYYY
    - DD-Mon-YYYY (3-letter month, case-insensitive)
    Requires 2-digit day and month (or 3-letter month) and 4-digit year.
    Validates calendar validity (leap years, 28/29/30/31 days).
    """
    if val is None:
        return None
    s = val.strip()

    year: int
    month: int
    day: int

    if m := _RE_ISO_DATE.match(s):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _RE_SLASH_DATE.match(s):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _RE_DASH_DATE.match(s):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := _RE_MON_DATE.match(s):
        day_str, mon_str, year_str = m.group(1), m.group(2).lower(), m.group(3)
        if mon_str not in MONTH_NAMES:
            return None
        day, month, year = int(day_str), MONTH_NAMES[mon_str], int(year_str)
    else:
        return None

    try:
        d = date(year, month, day)
        return d.isoformat()
    except (ValueError, OverflowError):
        return None


def parse_decimal(val: str | None) -> str | None:
    """Parse decimal amount and format with 2 decimal places using ROUND_HALF_UP.
    
    Accepts optional prefix ₹, Rs, Rs., INR (case-insensitive, optional space).
    Commas are thousands separators and are removed.
    Must be >= 0 and match digits (optional dot digits).
    Canonical format: string with exactly 2 decimal places (e.g. '25301.40').
    """
    if val is None:
        return None
    s = val.strip()
    if not s:
        return None

    # Remove currency prefix
    s = _RE_CURRENCY_PREFIX.sub("", s).strip()
    # Remove commas
    s = s.replace(",", "")

    if not _RE_DECIMAL_NUMBER.match(s):
        return None

    try:
        dec = Decimal(s)
        # Quantize to 2 places with ROUND_HALF_UP
        rounded = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{rounded:.2f}"
    except (InvalidOperation, ValueError):
        return None


def parse_enum(val: str | None, allowed_values: list[str]) -> str | None:
    """Parse enum value. Case-insensitively matched against allowed_values, stored lowercase."""
    if val is None:
        return None
    s = val.strip().lower()
    if not s:
        return None
    allowed_set = {v.lower() for v in allowed_values}
    if s in allowed_set:
        return s
    return None


def parse_timestamp(val: str | None) -> datetime | None:
    """Parse timestamp string into timezone-aware datetime.
    
    Accepts:
    - UTC ISO-8601 ending with 'Z' (e.g. 2026-10-05T13:20:21Z)
    - Offset ISO-8601 (e.g. 2026-10-05T18:50:21+05:30)
    - Naive datetime (e.g. 2026-10-05 18:50:21 or 2026-10-05T18:50:21), assumed to be IST (+05:30).
    Supports optional fractional seconds.
    Returns timezone-aware datetime or None if invalid.
    """
    if val is None:
        return None
    s = val.strip()
    if not s:
        return None

    # 1. UTC format with 'Z'
    if s.endswith("Z"):
        base = s[:-1]
        try:
            dt = datetime.fromisoformat(base)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # 2. Offset or naive ISO format
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Naive timestamp means IST
            return dt.replace(tzinfo=IST)
        return dt
    except ValueError:
        return None


def format_utc_timestamp(dt: datetime) -> str:
    """Format timezone-aware datetime as fixed-width UTC ISO-8601 string.
    
    Fixed-width: 'YYYY-MM-DDTHH:MM:SS.ffffffZ' so alphabetical order equals chronological order.
    """
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def format_ist_timestamp(dt: datetime) -> str:
    """Format timezone-aware datetime as ISO-8601 in IST with offset without microseconds.
    
    Example: '2026-10-14T17:55:24+05:30'.
    """
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def to_ist_date_str(dt: datetime) -> str:
    """Return the calendar date in IST as 'YYYY-MM-DD'."""
    ist_dt = dt.astimezone(IST)
    return ist_dt.date().isoformat()

