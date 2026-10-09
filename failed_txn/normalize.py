"""Reading what failed-transaction tickets and the bank's systems say: accounts, mobiles, references,
amounts (in any of the ways branches write them), dates and currencies."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from agentcore.normalize import (  # noqa: F401 - shared helpers, re-exported for this desk's modules
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
from agentcore.normalize import ACCOUNT_RE, MOBILE_RE, account_key, mobile_key  # noqa: F401 - shared with the desks

# A reference the client or branch quotes: "RRN 612345678901", "ref: ZP12345678", "Reference number 123456"
REFERENCE_RE = re.compile(r"(?i)\b(?:rrn|ref(?:erence)?(?:\s*(?:no|number|#))?|trace|stan|txn\s*id|transaction\s*id)"
                          r"\s*[:#.\-]?\s*([A-Za-z0-9]{6,32})\b")
CURRENCY_WORDS = r"(?:USD|US\$|\$|ZWG|ZIG|ZiG|ZWL|RTGS)"
AMOUNT_RE = re.compile(
    rf"(?i)(?:{CURRENCY_WORDS}\s?(\d[\d,\s]*(?:\.\d{{1,2}})?)|(\d[\d,]*(?:\.\d{{1,2}})?)\s?{CURRENCY_WORDS}(?![A-Za-z])"
    r"|\bamount\s*(?:of|is|was|:)?\s*(\d[\d,]*(?:\.\d{1,2})?))")
CURRENCIES = {"840": "USD", "USD": "USD", "US$": "USD", "$": "USD",
              "924": "ZWG", "932": "ZWG", "ZWG": "ZWG", "ZIG": "ZWG", "ZWL": "ZWL", "RTGS": "ZWG"}
CENT = Decimal("0.01")


def account_forms(value: Any) -> list[str]:
    """The forms an account may be stored in: as given, without leading zeros, and zero-padded to 14 digits."""
    digits = digits_only(value)
    key = digits.lstrip("0")
    if not key or len(key) > 20:
        return []
    forms = [digits, key, key.zfill(14)]
    return list(dict.fromkeys(f for f in forms if f))


def reference_key(value: Any) -> Optional[str]:
    text = re.sub(r"[\s-]", "", str(value or "")).upper()
    return text if re.fullmatch(r"[A-Z0-9]{6,32}", text) else None


def currency_name(code: Any) -> Optional[str]:
    text = str(code or "").strip().upper()
    return CURRENCIES.get(text, text or None)


def to_amount(value: Any) -> Optional[Decimal]:
    """A number as stored by a system (no thousands separators), in major units."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip()).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def parse_amount(text: Any) -> Optional[Decimal]:
    """An amount as people write it: 20, $20, USD 1,500.00, 1 500, ZWG1100, 1.500,00."""
    raw = re.sub(r"(?i)[a-z$\s]", "", str(text or ""))
    if not raw or not re.fullmatch(r"[\d.,]+", raw):
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):  # 1.500,00
            raw = raw.replace(".", "").replace(",", ".")
        else:  # 1,500.00
            raw = raw.replace(",", "")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = raw.replace(",", "") if len(tail) == 3 else f"{head.replace(',', '')}.{tail}"
    if raw.count(".") > 1:
        return None
    return to_amount(raw)


def find_amounts(text: str) -> set:
    """Amounts written with a currency or the word 'amount' (bare numbers are too ambiguous)."""
    found = set()
    for m in AMOUNT_RE.finditer(text or ""):
        value = parse_amount(next(g for g in m.groups() if g))
        if value is not None and value > 0:
            found.add(value)
    return found


def parse_day(value: Any) -> Optional[date]:
    """A date as written on tickets (day first, as in Zimbabwe) or by the systems (ISO)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"[T ]\d{1,2}:\d{2}.*$", "", text)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y", "%d %b %Y",
                "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    stamp = parse_timestamp(value)
    return stamp.date() if stamp else None


def money(amount: Optional[Decimal], currency: Optional[str] = None) -> str:
    if amount is None:
        return "an unknown amount"
    return f"{currency + ' ' if currency else ''}{amount:,.2f}"


def first(row: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    """The first non-empty value among likely column names (schemas differ between systems)."""
    for key in keys:
        if key in row:
            value = trim_to_none(row.get(key))
            if value is not None:
                return value
    return None


def format_timestamp(value: Optional[datetime]) -> str:
    return _format_timestamp(value, "")
