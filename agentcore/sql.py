"""SQL literal helpers, matching the Java agent.

Values are written into the SQL text, as the Java does, rather than bound as Metabase
parameters. Two reasons:
- Writes: Metabase runs parameterised native queries through a PreparedStatement with
  executeQuery(), which the MySQL driver refuses for INSERT/UPDATE. Without parameters it uses
  Statement.execute(), the write lands, and Metabase reports "did not produce a ResultSet"
  (which the client tolerates), exactly as the Java relies on.
- Reads: the core-banking queries were tuned and proven with literals on the standby DB.

Every identifier-like value is validated (digits only) before it is embedded, and free text is
escaped the way the Java escapes it.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_DIGITS = re.compile(r"^\d{1,32}$")


class UnsafeValue(ValueError):
    pass


def digits(value: Any) -> str:
    """A value that must be all digits (account numbers, phones, ticket numbers)."""
    text = str(value).strip()
    if not _DIGITS.match(text):
        raise UnsafeValue(f"expected digits only, got {text[:20]!r}")
    return text


def integer(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        raise UnsafeValue(f"expected an integer, got {str(value)[:20]!r}") from None


def oracle_text(value: str) -> str:
    """Oracle/PostgreSQL/SQL Server literal: double single quotes (as the Java agents do)."""
    return value.replace("'", "''")


def mysql_text(value: str) -> str:
    """MySQL literal (osTicket DB): escape backslashes, double single quotes (as the Java agents do)."""
    return value.replace("\\", "\\\\").replace("'", "''")


def like_fragment(value: str) -> str:
    """Text placed inside a LIKE '%...%' pattern: also neutralise the LIKE wildcards."""
    return value.replace("%", "").replace("_", "")


def oracle_date(value: date) -> str:
    """DD-MM-YYYY for TO_DATE(..., 'DD-MM-YYYY')."""
    return value.strftime("%d-%m-%Y")


def iso_date(value: date) -> str:
    return value.isoformat()


def pg_timestamptz(value: datetime) -> str:
    """TIMESTAMPTZ 'yyyy-MM-dd HH:mm:ss+HH:MM' as in SocketDeliveryChecker."""
    text = value.isoformat(sep=" ", timespec="seconds")
    return f"TIMESTAMPTZ '{text}'"
