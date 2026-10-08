"""Saving attempts to ecocash_monitor (PostgreSQL).

Two ways in, chosen with MONITOR_STORE:
- postgres (default): a direct connection, as the Java monitor had. The monitor runs on the
  integrations host, which already reaches the database. Credentials come from the env file
  (MONITOR_DB_URL, or the Java's DB_URL/DB_USERNAME/DB_PASSWORD), never from the code.
- metabase: literal SQL through the Metabase API with `RETURNING`, so no database credentials
  are needed on the host. Works only if Metabase's connection to ecocash_monitor may write.

Saving is idempotent: attempts are unique on attempt_key, so replaying the journal after a
restart never duplicates a row.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol
from urllib.parse import quote, unquote, urlparse

from agentcore import sql
from agentcore.metabase import MetabaseClient
from .parser import Attempt

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "sql" / "ecocash_monitor.sql"
COLUMNS = ("attempt_key", "operation", "outcome", "response_code", "message", "http_status", "channel", "via",
           "staff_user", "mobile_number", "account_number", "id_number", "currency", "first_name", "last_name",
           "ecocash_name", "ecocash_transaction_id", "external_reference", "requested_at", "responded_at", "host",
           "thread")
LEGACY_COLUMNS = ("mobile_number", "error_message", "id_number", "currency", "first_name", "last_name",
                  "account_number", "failure_timestamp")


def schema_statements() -> list[str]:
    text = re.sub(r"--[^\n]*", "", SCHEMA_FILE.read_text(encoding="utf-8"))
    return [s.strip() for s in text.split(";") if s.strip()]


def legacy_row(a: Attempt) -> Optional[dict[str, Any]]:
    """The Java monitor's row for a failed link (its columns are all NOT NULL)."""
    if a.operation != "LINK" or a.outcome != "failed":
        return None
    return {"mobile_number": a.mobile_number or "", "error_message": a.message or a.response_code or "unknown error",
            "id_number": a.id_number or "", "currency": a.currency or "", "first_name": a.first_name or "",
            "last_name": a.last_name or "", "account_number": a.account_number or "",
            "failure_timestamp": a.responded_at or a.requested_at}


def jdbc_to_conninfo(url: str, user: Optional[str], password: Optional[str]) -> str:
    """jdbc:postgresql://host:port/db?x=y (the Java's DB_URL) -> postgresql://user:pass@host:port/db?x=y"""
    if url.startswith("jdbc:"):
        url = url[len("jdbc:"):]
    parsed = urlparse(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("MONITOR_DB_URL / DB_URL must be a PostgreSQL URL")
    auth = ""
    user = user or (unquote(parsed.username) if parsed.username else None)
    password = password or (unquote(parsed.password) if parsed.password else None)
    if user:
        auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    return f"postgresql://{auth}{host}{port}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


class Store(Protocol):
    def ensure_schema(self) -> None: ...
    def save(self, attempts: Iterable[Attempt]) -> int: ...
    def close(self) -> None: ...


class PostgresStore:
    def __init__(self, conninfo: str, legacy: bool = True, connect: Any = None) -> None:
        self.conninfo = conninfo
        self.legacy = legacy
        self._connect = connect
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is None:
            connect = self._connect
            if connect is None:
                try:
                    import psycopg  # type: ignore

                    connect = psycopg.connect
                except ImportError:  # pragma: no cover - older servers
                    import psycopg2  # type: ignore

                    connect = psycopg2.connect
            self._conn = connect(self.conninfo)
        return self._conn

    def ensure_schema(self) -> None:
        conn = self._connection()
        with conn.cursor() as cur:
            for statement in schema_statements():
                cur.execute(statement)
        conn.commit()

    def save(self, attempts: Iterable[Attempt]) -> int:
        attempts = list(attempts)
        if not attempts:
            return 0
        conn = self._connection()
        inserted = 0
        try:
            with conn.cursor() as cur:
                for a in attempts:
                    row = a.as_row()
                    cur.execute(
                        f"INSERT INTO ecocash_link_attempts ({', '.join(COLUMNS)}) "
                        f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) ON CONFLICT (attempt_key) DO NOTHING RETURNING id",
                        [_clean(row[c]) for c in COLUMNS])
                    if cur.fetchone() is None:
                        continue  # already saved (replay after a restart)
                    inserted += 1
                    legacy = legacy_row(a) if self.legacy else None
                    if legacy:
                        cur.execute(f"INSERT INTO failed_ecocash_registrations ({', '.join(LEGACY_COLUMNS)}) "
                                    f"VALUES ({', '.join(['%s'] * len(LEGACY_COLUMNS))})",
                                    [_clean(legacy[c]) for c in LEGACY_COLUMNS])
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            finally:
                self.close()  # reconnect next time
            raise
        return inserted

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


class MetabaseStore:
    def __init__(self, metabase: MetabaseClient, database_id: int, legacy: bool = True) -> None:
        self.mb = metabase
        self.db = database_id
        self.legacy = legacy

    def ensure_schema(self) -> None:
        for statement in schema_statements():
            self.mb.write(self.db, statement)

    def save(self, attempts: Iterable[Attempt]) -> int:
        inserted = 0
        for a in attempts:
            row = a.as_row()
            values = ", ".join(literal(row[c]) for c in COLUMNS)
            rows = self.mb.query(self.db, f"INSERT INTO ecocash_link_attempts ({', '.join(COLUMNS)}) VALUES ({values}) "
                                          "ON CONFLICT (attempt_key) DO NOTHING RETURNING id")
            if not rows:
                continue
            inserted += 1
            legacy = legacy_row(a) if self.legacy else None
            if legacy:
                lv = ", ".join(literal(legacy[c]) for c in LEGACY_COLUMNS)
                self.mb.query(self.db, f"INSERT INTO failed_ecocash_registrations ({', '.join(LEGACY_COLUMNS)}) "
                                       f"VALUES ({lv}) RETURNING id")
        return inserted

    def close(self) -> None:
        pass


class MemoryStore:
    """For --dry-run and tests."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.legacy: list[dict[str, Any]] = []

    def ensure_schema(self) -> None:
        pass

    def save(self, attempts: Iterable[Attempt]) -> int:
        inserted = 0
        for a in attempts:
            if a.key in self.rows:
                continue
            self.rows[a.key] = a.as_row()
            inserted += 1
            legacy = legacy_row(a)
            if legacy:
                self.legacy.append(legacy)
        return inserted

    def close(self) -> None:
        pass


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def literal(value: Any) -> str:
    """A PostgreSQL literal (standard_conforming_strings: only quotes are doubled)."""
    from datetime import datetime

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return "TIMESTAMP '" + value.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    return "'" + sql.oracle_text(_clean(str(value))) + "'"
