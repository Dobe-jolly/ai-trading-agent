"""Reading what NMB Connect tickets and the bank's systems say: CIFs, accounts, mobiles, national IDs, e-mails."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from agentcore.normalize import (  # noqa: F401 - shared helpers, re-exported for this desk's modules
    ACCOUNT_RE,
    ID_RE,
    LOCAL_ZONE,
    MOBILE_RE,
    NON_DIGITS,
    account_key,
    digits_only,
    id_key,
    mobile_key,
    normalize_account_number,
    now_local,
    parse_timestamp,
    today_local,
    trim_to_none,
    window,
)
from agentcore.normalize import format_timestamp as _format_timestamp

# "CIF 1234567", "CIF no: 1234567", "cif-1234567", "Customer number 1234567"
CIF_RE = re.compile(r"(?i)\b(?:cif|customer\s*(?:no|number|#)|client\s*(?:no|number|#))\s*(?:no\.?|number|#)?"
                    r"\s*[:#=.\-]?\s*(\d{4,12})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def cif_key(value: Any) -> Optional[str]:
    """A CIF (customer number) as digits without leading zeros; None if it cannot be one."""
    digits = digits_only(value).lstrip("0")
    return digits if 3 <= len(digits) <= 12 else None


def email_key(value: Any) -> Optional[str]:
    m = EMAIL_RE.search(str(value or ""))
    return m.group(0).lower() if m else None


def format_timestamp(value: Optional[datetime]) -> str:
    return _format_timestamp(value, "")


def day_text(value: Optional[datetime]) -> str:
    """dd/mm/YYYY, as branches write dates."""
    return value.astimezone(LOCAL_ZONE).strftime("%d/%m/%Y") if value else "an unknown date"


def truthy(value: Any) -> Optional[bool]:
    """A flag as core banking and the app store it ('1', 'Y', 'T', 'true', 1, True); None when unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "y", "yes", "t", "true"):
        return True
    if text in ("0", "n", "no", "f", "false"):
        return False
    return None
