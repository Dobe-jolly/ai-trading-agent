"""Normalising the identifiers EcoCash tickets and link attempts carry: mobile numbers, accounts,
national IDs, currencies and times."""
from __future__ import annotations

from datetime import date, datetime
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
from agentcore.normalize import (  # noqa: F401 - client identifiers, shared with the other desks
    ACCOUNT_RE,
    ID_RE,
    MOBILE_RE,
    NETWORKS,
    account_key,
    id_key,
    mobile_key,
    normalize_account_number,
)


def international_mobile(value: Any) -> Optional[str]:
    key = mobile_key(value)
    return f"+263{key}" if key else None


def network_of(value: Any) -> str:
    key = mobile_key(value)
    return NETWORKS.get(key[:2], "unknown") if key else "unknown"


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
