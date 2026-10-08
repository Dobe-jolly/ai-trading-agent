"""Normalisation rules ported 1:1 from the Java SMS agent.

Two phone normalisers exist on purpose, as in the Java:
- normalize_ticket_phone: SmsAgentService.normalizePhone (lenient; used for ticket vs Intellect comparison)
- normalize_gateway_phone: SocketMessageParser.normalizePhone (strict; rejects placeholders; used for
  gateway reconciliation and must stay identical to the queue-monitor agent)
"""
from __future__ import annotations

import re
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


PLACEHOLDER_EMAILS = {"none@nmbz.co.zw", "nmblite@nmbz.co.zw"}

ALERT_SALARIES = "SALARIES"
ALERT_DEBITS = "DEBITS"


# ---- SmsAgentService helpers ----------------------------------------------------
def normalize_ticket_phone(raw: Any) -> Optional[str]:
    text = trim_to_none(raw)
    if text is None:
        return None
    digits = digits_only(text)
    if not digits:
        return None
    if digits.startswith("2637") and len(digits) == 12:
        return digits
    if digits.startswith("07") and len(digits) == 10:
        return "263" + digits[1:]
    if digits.startswith("7") and len(digits) == 9:
        return "263" + digits
    return digits


def is_valid_ticket_phone(raw: Any) -> bool:
    normalized = normalize_ticket_phone(raw)
    return normalized is not None and normalized.startswith("2637") and len(normalized) == 12


def is_deliverable_phone(normalized: Optional[str]) -> bool:
    return (
        normalized is not None
        and normalized.startswith("2637")
        and len(normalized) == 12
        and not normalized.endswith("000000")
    )


def normalize_account_number(raw: Any) -> Optional[str]:
    text = trim_to_none(raw)
    if text is None:
        return None
    digits = digits_only(text)
    if not digits:
        return None
    return digits.zfill(14) if len(digits) < 14 else digits


def is_sms_activated(value: Any) -> Optional[bool]:
    text = trim_to_none(value)
    if text is None:
        return None
    text = text.lower()
    if text in ("1", "true", "t", "y", "yes"):
        return True
    if text in ("0", "false", "f", "n", "no"):
        return False
    return None


def parse_alert_flag(raw: Any) -> Optional[bool]:
    """AlertRegistration.parseAlertFlag."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "y", "yes", "t"):
        return True
    if text in ("false", "0", "n", "no", "f"):
        return False
    return None


def is_usable_customer_email(email: Any) -> bool:
    if email is None:
        return False
    normalized = str(email).strip().lower()
    if not normalized or "@" not in normalized:
        return False
    return normalized not in PLACEHOLDER_EMAILS


def uses_both_accounts(currency: Any) -> bool:
    normalized = (trim_to_none(currency) or "").upper()
    if not normalized:
        return False
    if normalized == "BOTH":
        return True
    tokens = [t for t in re.split(r"[^A-Z0-9]+", normalized) if t]
    return "USD" in tokens and ("ZWG" in tokens or "ZIG" in tokens)


def requested_alert_types(ticket_type: Any, help_topic: Any) -> list[str]:
    """SmsAgentService.requestedAlertTypes: substring match on the 'Salaries or Debits' field and topic."""
    kind = (trim_to_none(ticket_type) or "").lower()
    topic = (trim_to_none(help_topic) or "").lower()
    type_salaries = "salar" in kind or "credit" in kind
    type_debits = "debit" in kind
    both = "both" in kind or (type_salaries and type_debits)
    wants_salaries = both or type_salaries or "salaries" in topic
    wants_debits = both or type_debits or "transactional" in topic
    types: list[str] = []
    if wants_salaries:
        types.append(ALERT_SALARIES)
    if wants_debits:
        types.append(ALERT_DEBITS)
    return types or [ALERT_DEBITS]


def ticket_account_numbers(account: Optional[str], account_2: Optional[str], currency: Any) -> list[str]:
    """SmsAgentService.getTicketAccountNumbers (inputs already normalised)."""
    accounts: list[str] = []
    if account:
        accounts.append(account)
    if uses_both_accounts(currency) and account_2 and account_2 not in accounts:
        accounts.append(account_2)
    return accounts


# ---- SocketMessageParser helpers ------------------------------------------------
_PHONE_263 = re.compile(r"2637\d{8}")
_PHONE_0 = re.compile(r"07\d{8}")
_PHONE_7 = re.compile(r"7\d{8}")
_PLACEHOLDERS = (re.compile(r"2637\d{2}000000"), re.compile(r"07\d{2}000000"), re.compile(r"7\d{2}000000"))
ACCOUNT_SUFFIX_PATTERN = re.compile(r"Account(?:\s+No\.?)?\s+[^0-9]*([0-9]{7,})", re.I)
AMOUNT_PATTERN = re.compile(r"(USD|ZWG|ZIG)\s+([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d+)?|[0-9]+(?:\.\d+)?)", re.I)


def _last_overlapping_match(text: str, pattern: re.Pattern[str]) -> Optional[str]:
    found = None
    for index in range(len(text)):
        match = pattern.match(text, index)
        if match:
            found = match.group()
    return found


def _is_placeholder(digits: str) -> bool:
    return any(p.fullmatch(digits) for p in _PLACEHOLDERS)


def normalize_gateway_phone(value: Any) -> Optional[str]:
    if value is None:
        return None
    digits = digits_only(value)
    if not digits:
        return None
    match = _last_overlapping_match(digits, _PHONE_263)
    if match:
        return None if _is_placeholder(match) else match
    match = _last_overlapping_match(digits, _PHONE_0)
    if match:
        return None if _is_placeholder(match) else "263" + match[1:]
    match = _last_overlapping_match(digits, _PHONE_7)
    if match:
        return None if _is_placeholder(match) else "263" + match
    return None


def subscriber(value: Any) -> Optional[str]:
    """The 9-digit subscriber core (7XXXXXXXX), or None."""
    if value is None:
        return None
    digits = digits_only(value)
    if len(digits) < 9:
        return None
    tail = digits[-9:]
    return tail if tail[0] == "7" else None


def normalize_account_suffix(value: Any) -> Optional[str]:
    if value is None:
        return None
    digits = digits_only(value)
    if not digits:
        return None
    return digits if len(digits) <= 7 else digits[-7:]


def normalize_currency(value: Any) -> Optional[str]:
    text = trim_to_none(value)
    if text is None:
        return None
    text = text.upper()
    if text in ("840", "USD"):
        return "USD"
    if text in ("924", "932", "ZWG", "ZIG"):
        return "ZIG"
    return text


def currencies_match(left: Any, right: Any) -> bool:
    a, b = normalize_currency(left), normalize_currency(right)
    return a is not None and a == b


def to_decimal(value: Any) -> Optional[Decimal]:
    text = trim_to_none(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def amounts_match(left: Optional[Decimal], right: Optional[Decimal]) -> bool:
    return left is not None and right is not None and left == right


def parse_date(value: Any) -> Optional[date]:
    """Loose date parse for a transaction date the model passes (YYYY-MM-DD or DD-MM-YYYY or DD/MM/YYYY)."""
    text = trim_to_none(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def format_timestamp(value: Optional[datetime]) -> str:
    return _format_timestamp(value, 'unknown time')
