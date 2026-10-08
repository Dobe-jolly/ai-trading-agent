"""Normalising the identifiers EcoCash tickets and link attempts carry: mobile numbers, accounts,
national IDs, currencies and times."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from agentcore.normalize import (  # noqa: F401 - shared helpers, re-exported for this agent's modules
    LOCAL_ZONE,
    NON_DIGITS,
    digits_only,
    now_local,
    parse_timestamp,
    today_local,
    trim_to_none,
    window,
)
from agentcore.normalize import format_timestamp as _format_timestamp


MOBILE_RE = re.compile(r"(?<![\d+])(?:\+?263|0)?[\s-]?(7[1378])(?:[\s-]?\d){7}(?!\d)")
ID_RE = re.compile(r"(?<![\w-])(\d{2})[\s-]?(\d{6,7})[\s-]?([A-Za-z])[\s-]?(\d{2})(?![\w])")
ACCOUNT_RE = re.compile(r"(?<![\d+])\d(?:[\s-]?\d){9,15}(?!\d)")
NETWORKS = {"71": "NetOne", "73": "Telecel", "77": "Econet", "78": "Econet"}


def mobile_key(value: Any) -> Optional[str]:
    """The last nine digits of a Zimbabwe mobile (772123456), whatever the format; None if not one."""
    digits = digits_only(value)
    if digits.startswith("263"):
        digits = digits[3:]
    digits = digits.lstrip("0")
    if len(digits) != 9 or digits[:2] not in NETWORKS:
        return None
    return digits


def international_mobile(value: Any) -> Optional[str]:
    key = mobile_key(value)
    return f"+263{key}" if key else None


def network_of(value: Any) -> str:
    key = mobile_key(value)
    return NETWORKS.get(key[:2], "unknown") if key else "unknown"


def account_key(value: Any) -> Optional[str]:
    """Account digits without leading zeros (0086000013101001 and 86000013101001 are one account)."""
    digits = digits_only(value).lstrip("0")
    return digits if 6 <= len(digits) <= 20 else None


def normalize_account_number(value: Any) -> Optional[str]:
    """14-digit form used by core banking (IACLINK_ACTUAL_ACNUM), as the SMS agent and the Java use it."""
    digits = digits_only(value)
    if not digits or len(digits) > 20:
        return None
    return digits.zfill(14) if len(digits) < 14 else digits


def id_key(value: Any) -> Optional[str]:
    """A Zimbabwe national ID without separators, upper case (58-7654321 F 09 -> 587654321F09)."""
    m = ID_RE.search(str(value or ""))
    if not m:
        return None
    return "".join(m.groups()).upper()


def normalize_currency(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if text in ("840", "USD", "US$"):
        return "USD"
    if text in ("924", "932", "ZWG", "ZIG", "ZWL", "RTGS"):
        return "ZWG" if text != "ZWL" else "ZWL"
    return text or None


def to_decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    stamp = parse_timestamp(value)
    return stamp.date() if stamp else None


def format_timestamp(value: Optional[datetime]) -> str:
    return _format_timestamp(value, '')
