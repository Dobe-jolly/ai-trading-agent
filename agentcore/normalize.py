"""Normalising helpers both agents share: local time, timestamps, digits."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

LOCAL_ZONE = ZoneInfo("Africa/Johannesburg")  # CAT/SAST, UTC+2 all year, same as Harare
NON_DIGITS = re.compile(r"\D+")


def digits_only(value: Any) -> str:
    return NON_DIGITS.sub("", str(value or ""))


def now_local() -> datetime:
    return datetime.now(LOCAL_ZONE)


def today_local() -> date:
    return datetime.now(LOCAL_ZONE).date()


def trim_to_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def window(end: date, days: int) -> tuple[date, date]:
    return end - timedelta(days=days), end


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Timestamps from Metabase (Oracle, PostgreSQL, MySQL) and the logs. Values without a zone are local time
    (Africa/Johannesburg); numbers are epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=LOCAL_ZONE)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(LOCAL_ZONE)
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        candidate += "T00:00:00"
    candidate = re.sub(r"^(\d{4}-\d{2}-\d{2}) ", r"\1T", candidate)
    # Python 3.9's fromisoformat only accepts 3 or 6 fractional digits.
    candidate = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], candidate)
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_ZONE)
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_ZONE)
        except ValueError:
            continue
    return None


def format_timestamp(value: Optional[datetime], empty: str = "") -> str:
    """dd-mm-YYYY HH:MM in local time, as the branches read dates."""
    if value is None:
        return empty
    return value.astimezone(LOCAL_ZONE).strftime("%d-%m-%Y %H:%M")
