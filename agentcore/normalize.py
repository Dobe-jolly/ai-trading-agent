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


# ---- client identifiers as tickets and the bank's systems write them (shared by the desks) -------------------
MOBILE_RE = re.compile(r"(?<![\d+])(?:\+?263|0)?[\s-]?(7[1378])(?:[\s-]?\d){7}(?!\d)")
ACCOUNT_RE = re.compile(r"(?<![\d+])\d(?:[\s-]?\d){9,15}(?!\d)")
ID_RE = re.compile(r"(?<![\w-])(\d{2})[\s-]?(\d{6,7})[\s-]?([A-Za-z])[\s-]?(\d{2})(?![\w])")
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


def account_key(value: Any) -> Optional[str]:
    """Account digits without leading zeros (00000232728329 and 232728329 are one account)."""
    digits = digits_only(value).lstrip("0")
    return digits if 6 <= len(digits) <= 20 else None


def id_key(value: Any) -> Optional[str]:
    """A Zimbabwe national ID without separators, upper case (58-7654321 F 09 -> 587654321F09)."""
    m = ID_RE.search(str(value or ""))
    if not m:
        return None
    return "".join(m.groups()).upper()


def normalize_account_number(value: Any) -> Optional[str]:
    """14-digit form used by core banking (IACLINK_ACTUAL_ACNUM), as the SMS agent and the Java use it."""
    digits = digits_only(value)
    if not digits or len(digits) > 20:
        return None
    return digits.zfill(14) if len(digits) < 14 else digits

