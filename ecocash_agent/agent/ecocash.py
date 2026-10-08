"""EcoCash account linking: what each EcoCash response means, who fixes it, the standard reply to
the branch, and the lookups behind the agent's tools.

Data (all read through Metabase):
- ecocash_monitor (PostgreSQL): ecocash_link_attempts, every link and unlink request and its
  outcome, written by the Python monitor (agent/monitor); and failed_ecocash_registrations, the
  Java monitor's failures-only table, for history from before the switch.
- Postilion (SQL Server): eftc_ecocash, the bank's record of each successful link.
- Core banking (STANDBY DB, Oracle): whether the account exists, its currency and the mobile on file.

Codes and counts are from the registration log of 6 April 2026 (16:16-23:34): 134 requests
reached EcoCash, 61 linked and 73 failed (47 of them 20503).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    LOCAL_ZONE,
    account_key,
    format_timestamp,
    id_key,
    mobile_key,
    normalize_account_number,
    now_local,
    parse_timestamp,
    trim_to_none,
)


@dataclass(frozen=True)
class ErrorType:
    key: str
    label: str
    ecocash_code: str
    pattern: str  # case-insensitive text in EcoCash's message (fallback when the code is missing)
    who_fixes: str  # branch | client | client_or_branch | retry | ict
    meaning: str
    guidance: str  # the standard reply to the branch; {mobile} and {account} are filled in


ERROR_TYPES: tuple[ErrorType, ...] = (
    ErrorType(
        "already_linked", "Account already linked to an EcoCash wallet", "20503", "already exists in the identity",
        "branch",
        "The bank account is already linked to an EcoCash wallet (an earlier link, possibly on another number), "
        "so EcoCash refused a second link.",
        "EcoCash rejected the link because account {account} is already linked to an EcoCash wallet (EcoCash error "
        "20503). Kindly delink the account on the Universal dashboard, then ask the client to link it again. If the "
        "delink fails, reply on this ticket with a screenshot of the error message and ICT will delink it.",
    ),
    ErrorType(
        "kyc_mismatch", "KYC verification failed", "BCS50646", "KYC verification failed",
        "client_or_branch",
        "EcoCash compared the name and national ID the bank sent with its own KYC records and they differ.",
        "EcoCash rejected the link because the client's details do not match EcoCash's KYC records (EcoCash error "
        "BCS50646).",
    ),
    ErrorType(
        "number_not_ecocash", "Number not registered on EcoCash", "TP40288", "specified number does not exist",
        "client",
        "The mobile number used has no EcoCash wallet (not registered, or not an Econet line).",
        "EcoCash rejected the link because {mobile} is not registered on EcoCash (EcoCash error TP40288). Kindly "
        "advise the client to register for EcoCash on that number, or to link using the number their EcoCash wallet "
        "is on.",
    ),
    ErrorType(
        "wallet_closed", "EcoCash wallet closed", "TP40050", "receiver is in the Closed state",
        "client",
        "The client's EcoCash wallet is closed, so it cannot be linked.",
        "EcoCash rejected the link because the client's EcoCash wallet on {mobile} is closed (EcoCash error "
        "TP40050). Kindly advise the client to visit an EcoCash shop to reactivate the wallet, then link the "
        "account again.",
    ),
    ErrorType(
        "ecocash_system_error", "EcoCash system error", "TP40024", "Internal System error",
        "retry",
        "A temporary error on EcoCash's side.",
        "EcoCash returned a temporary system error when the client tried to link the account (EcoCash error "
        "TP40024). Kindly ask the client to try again in a few hours.",
    ),
    ErrorType(
        "account_rejected", "Account number rejected by EcoCash", "TC30174", "AccountNumber parameter is incorrect",
        "ict",
        "EcoCash did not accept the account number in a delink request. The registration service sends the "
        "client's mobile number in EcoCash's account-number field (it does for links too, which EcoCash accepts); "
        "both branch delinks in the 6 April 2026 log were rejected this way, so ICT has to delink.",
        "",
    ),
)
UNKNOWN = ErrorType("unknown", "Unrecognised EcoCash error", "", "", "ict",
                    "An EcoCash error the agent has no rule for.", "")
NO_RESPONSE = ErrorType("no_response", "No answer from EcoCash", "", "", "ict",
                        "The request was logged but no answer from EcoCash was (timeout or service error).", "")
BY_KEY = {e.key: e for e in ERROR_TYPES + (UNKNOWN, NO_RESPONSE)}
BY_CODE = {e.ecocash_code: e for e in ERROR_TYPES}
# A ticket can be closed on these once the branch has the answer (subject to the rules in code).
CLOSABLE = {"linked", "number_not_ecocash", "wallet_closed", "kyc_mismatch", "ecocash_system_error"}
# These always need a person.
ESCALATING = {"account_rejected", "unknown", "no_response"}
VIA_LABELS = {"USSD": "client on USSD", "VBS": "branch staff", "IAPPROVE": "account opening (iApprove)"}


def classify(code: Any, message: Any) -> ErrorType:
    code = str(code or "").strip()
    if code in BY_CODE:
        return BY_CODE[code]
    text = str(message or "").lower()
    for error in ERROR_TYPES:
        if error.pattern.lower() in text:
            return error
    return UNKNOWN


def clean_message(message: Any, error: ErrorType) -> str:
    """EcoCash's message without the wallet holder's name (TP40050 embeds it)."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if error.key == "wallet_closed":
        return "The receiver is in the Closed state which does not support the current service."
    return text[:300]


def classify_sql(code_column: str = "response_code", message_column: str = "message") -> str:
    """The same classification in SQL (PostgreSQL), for aggregates."""
    cases = " ".join(f"WHEN {code_column} = '{e.ecocash_code}' THEN '{e.key}'" for e in ERROR_TYPES)
    texts = " ".join(f"WHEN {message_column} ILIKE '%{sql.like_fragment(sql.oracle_text(e.pattern))}%' THEN '{e.key}'"
                     for e in ERROR_TYPES)
    return f"CASE {cases} {texts} ELSE 'unknown' END"


def _ts_literal(value: datetime) -> str:
    """The monitor stores local times without a zone."""
    return "TIMESTAMP '" + value.astimezone(LOCAL_ZONE).strftime("%Y-%m-%d %H:%M:%S") + "'"


# ---- link attempts -------------------------------------------------------------------------
@dataclass
class LinkAttempt:
    id: str  # "a123" (attempts table) or "f45" (the Java's failures table)
    when: Optional[datetime]
    operation: str  # LINK | UNLINK
    outcome: str  # linked | unlinked | failed | no_response
    error: Optional[ErrorType]  # None when it succeeded
    message: str
    via: Optional[str]
    mobile: Optional[str]
    account: Optional[str]
    id_number: Optional[str]
    currency: Optional[str]
    first_name: Optional[str] = None  # never shown to the model: only whether it matches the ticket
    last_name: Optional[str] = None
    ecocash_name: Optional[str] = None
    source: str = "attempts"

    @property
    def type_key(self) -> str:
        if self.outcome in ("linked", "unlinked"):
            return self.outcome
        return self.error.key if self.error else "unknown"

    @classmethod
    def from_attempt_row(cls, row: dict[str, Any]) -> "LinkAttempt":
        outcome = str(row.get("outcome") or "failed")
        error = None
        if outcome == "no_response":
            error = NO_RESPONSE
        elif outcome == "failed":
            error = classify(row.get("response_code"), row.get("message"))
        return cls(
            id=f"a{row.get('id')}", when=parse_timestamp(row.get("requested_at")),
            operation=str(row.get("operation") or "LINK"), outcome=outcome, error=error,
            message=clean_message(row.get("message"), error) if error else str(row.get("message") or "")[:200],
            via=trim_to_none(row.get("via")), mobile=trim_to_none(row.get("mobile_number")),
            account=trim_to_none(row.get("account_number")), id_number=trim_to_none(row.get("id_number")),
            currency=trim_to_none(row.get("currency")), first_name=trim_to_none(row.get("first_name")),
            last_name=trim_to_none(row.get("last_name")), ecocash_name=trim_to_none(row.get("ecocash_name")))

    @classmethod
    def from_legacy_row(cls, row: dict[str, Any]) -> "LinkAttempt":
        error = classify(None, row.get("error_message"))
        return cls(
            id=f"f{row.get('id')}", when=parse_timestamp(row.get("failure_timestamp")), operation="LINK",
            outcome="failed", error=error, message=clean_message(row.get("error_message"), error), via=None,
            mobile=trim_to_none(row.get("mobile_number")), account=trim_to_none(row.get("account_number")),
            id_number=trim_to_none(row.get("id_number")), currency=trim_to_none(row.get("currency")),
            first_name=trim_to_none(row.get("first_name")), last_name=trim_to_none(row.get("last_name")),
            source="java_failures")

    def describe(self, ticket_text: str = "") -> dict[str, Any]:
        """What the model sees (masked by the caller). Names never: only whether they match the ticket."""
        error = self.error
        out: dict[str, Any] = {
            "attempt_id": self.id,
            "when": format_timestamp(self.when),
            "request": "unlink (delink)" if self.operation == "UNLINK" else "link",
            "outcome": self.outcome,
            "requested_by": VIA_LABELS.get(self.via or "", self.via or "unknown"),
            "mobile_used": self.mobile,
            "account_used": self.account,
            "national_id_sent": self.id_number,
            "currency": self.currency,
        }
        if error is not None:
            out.update({
                "error_type": error.key,
                "error": f"{error.label} (EcoCash {error.ecocash_code})" if error.ecocash_code else error.label,
                "ecocash_message": self.message,
                "meaning": error.meaning,
                "who_fixes": error.who_fixes,
            })
        haystack = ticket_text.upper()
        names = [n for n in (self.first_name, self.last_name) if n]
        out["name_sent_matches_ticket"] = (all(re.search(rf"\b{re.escape(n.upper())}\b", haystack) for n in names)
                                           if names and haystack else None)
        sent_id = id_key(self.id_number)
        ticket_ids = {id_key(m.group(0)) for m in re.finditer(r"\d{2}[\s-]?\d{6,7}[\s-]?[A-Za-z][\s-]?\d{2}",
                                                               ticket_text)} - {None}
        out["id_sent_matches_an_id_on_ticket"] = (sent_id in ticket_ids) if sent_id and ticket_ids else None
        if self.ecocash_name:
            wallet = [w for w in re.split(r"\s+", self.ecocash_name.upper()) if len(w) > 1]
            out["ecocash_wallet_name_matches_ticket"] = (all(re.search(rf"\b{re.escape(w)}\b", haystack) for w in wallet)
                                                         if wallet and haystack else None)
        return out


@dataclass
class Lookup:
    """The attempts for one search, newest first, and what they add up to."""
    attempts: list[LinkAttempt] = field(default_factory=list)

    @property
    def latest(self) -> Optional[LinkAttempt]:
        return self.attempts[0] if self.attempts else None

    @property
    def latest_link(self) -> Optional[LinkAttempt]:
        return next((a for a in self.attempts if a.operation == "LINK"), None)

    @property
    def status(self) -> str:
        """linked | unlinked | <error type> | none: the state the latest attempt leaves the client in."""
        latest = self.latest
        if latest is None:
            return "none"
        return latest.type_key

    def system_errors_since(self, since: datetime) -> int:
        return sum(1 for a in self.attempts if a.error and a.error.key == "ecocash_system_error"
                   and a.when and a.when >= since)


class AttemptStore:
    """ecocash_link_attempts (and the Java's failed_ecocash_registrations) through Metabase."""

    ATTEMPT_COLUMNS = ("id, operation, outcome, response_code, message, via, mobile_number, account_number, "
                       "id_number, currency, first_name, last_name, ecocash_name, requested_at")
    LEGACY_COLUMNS = ("id, mobile_number, error_message, id_number, currency, first_name, last_name, "
                      "account_number, failure_timestamp")

    def __init__(self, metabase: MetabaseClient, database_id: int, *, attempts_table: str = "ecocash_link_attempts",
                 legacy_table: Optional[str] = "failed_ecocash_registrations") -> None:
        for table in (attempts_table, legacy_table):
            if table and not re.fullmatch(r"[A-Za-z_][\w.]*", table):
                raise ValueError(f"unsafe table name {table!r}")
        self.mb = metabase
        self.db = database_id
        self.attempts_table = attempts_table
        self.legacy_table = legacy_table
        self._attempts_start: Optional[datetime] = None
        self._start_known = False

    @staticmethod
    def conditions(mobile: Optional[str], account: Optional[str], id_number: Optional[str]) -> list[str]:
        m, a, i = mobile_key(mobile), account_key(account), id_key(id_number)
        if mobile is not None and m is None:
            raise ValueError("mobile must be a Zimbabwe mobile number")
        if account is not None and a is None:
            raise ValueError("account_number must contain the account digits")
        if id_number is not None and i is None:
            raise ValueError("id_number must be a Zimbabwe national ID such as 63-123456A78")
        out = []
        if m:
            out.append(f"RIGHT(regexp_replace(mobile_number, '\\D', '', 'g'), 9) = '{sql.digits(m)}'")
        if a:
            out.append(f"ltrim(regexp_replace(account_number, '\\D', '', 'g'), '0') = '{sql.digits(a)}'")
        if i:
            out.append(f"upper(regexp_replace(id_number, '[^A-Za-z0-9]', '', 'g')) = '{sql.oracle_text(i)}'")
        if not out:
            raise ValueError("give at least one of mobile, account_number or id_number")
        return out

    def attempts_start(self) -> Optional[datetime]:
        """When the Python monitor's records begin (the Java's failures fill in before that)."""
        if not self._start_known:
            rows = self.mb.query(self.db, f"SELECT MIN(requested_at) AS first_at FROM {self.attempts_table}")
            self._attempts_start = parse_timestamp(rows[0].get("first_at")) if rows else None
            self._start_known = True
        return self._attempts_start

    def find(self, *, mobile: Optional[str] = None, account: Optional[str] = None, id_number: Optional[str] = None,
             days: int = 60, as_of: Optional[datetime] = None, limit: int = 30) -> Lookup:
        """Attempts for any of the identifiers (OR), newest first."""
        where = " OR ".join(self.conditions(mobile, account, id_number))
        end = as_of or now_local()
        start = end - timedelta(days=days)
        rows = self.mb.query(self.db, (
            f"SELECT {self.ATTEMPT_COLUMNS} FROM {self.attempts_table} WHERE ({where}) "
            f"AND requested_at >= {_ts_literal(start)} AND requested_at <= {_ts_literal(end)} "
            f"ORDER BY requested_at DESC, id DESC LIMIT {sql.integer(limit)}"))
        found = [LinkAttempt.from_attempt_row(r) for r in rows]
        if self.legacy_table:
            first = self.attempts_start()
            legacy_end = min(end, first) if first else end
            if legacy_end > start:
                legacy_where = where.replace("requested_at", "failure_timestamp")
                rows = self.mb.query(self.db, (
                    f"SELECT {self.LEGACY_COLUMNS} FROM {self.legacy_table} WHERE ({legacy_where}) "
                    f"AND failure_timestamp >= {_ts_literal(start)} AND failure_timestamp < {_ts_literal(legacy_end)} "
                    f"ORDER BY failure_timestamp DESC, id DESC LIMIT {sql.integer(limit)}"))
                found += [LinkAttempt.from_legacy_row(r) for r in rows]
        found.sort(key=lambda a: a.when or datetime.min.replace(tzinfo=LOCAL_ZONE), reverse=True)
        return Lookup(found[:limit])

    def counts(self, start: datetime, end: datetime, by_hour: bool) -> list[dict[str, Any]]:
        """Outcome counts per hour or day, by type and channel (anomaly watch)."""
        bucket = "date_trunc('hour', requested_at)" if by_hour else "date_trunc('day', requested_at)"
        kind = (f"CASE WHEN outcome IN ('linked', 'unlinked') THEN outcome WHEN outcome = 'no_response' "
                f"THEN 'no_response' ELSE {classify_sql()} END")
        return self.mb.query(self.db, (
            f"SELECT {bucket} AS bucket, operation, COALESCE(via, 'unknown') AS via, {kind} AS kind, COUNT(*) AS n "
            f"FROM {self.attempts_table} WHERE requested_at >= {_ts_literal(start)} AND requested_at < {_ts_literal(end)} "
            "GROUP BY 1, 2, 3, 4"))

    def sample(self, start: datetime, end: datetime, outcome_sql: str, limit: int) -> list[dict[str, Any]]:
        """Recent attempts (no names) for the failed-vs-linked comparison."""
        return self.mb.query(self.db, (
            "SELECT operation, outcome, response_code, message, via, channel, mobile_number, account_number, "
            f"id_number, currency, requested_at, responded_at FROM {self.attempts_table} "
            f"WHERE requested_at >= {_ts_literal(start)} AND requested_at < {_ts_literal(end)} AND {outcome_sql} "
            f"ORDER BY requested_at DESC LIMIT {sql.integer(limit)}"))


# ---- the bank's link record (Postilion) ------------------------------------------------------
class LinkRecords:
    """eftc_ecocash in Postilion (SQL Server): written by ecocashreg.service after a successful link."""

    COLUMNS = "date_added, created_by, msisdn, account_id, currency_code, status"

    def __init__(self, metabase: MetabaseClient, database_id: int, table: str = "eftc_ecocash") -> None:
        if not re.fullmatch(r"[A-Za-z_][\w.\[\]]*", table):
            raise ValueError(f"unsafe table name {table!r}")
        self.mb = metabase
        self.db = database_id
        self.table = table

    def find(self, *, mobile: Optional[str] = None, account: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        m, a = mobile_key(mobile), account_key(account)
        if mobile is not None and m is None:
            raise ValueError("mobile must be a Zimbabwe mobile number")
        if account is not None and a is None:
            raise ValueError("account_number must contain the account digits")
        conditions = []
        if m:
            conditions.append(f"msisdn LIKE '%{sql.digits(m)}'")
        if a:
            conditions.append(f"account_id LIKE '%{sql.digits(a[-10:])}'")
        if not conditions:
            raise ValueError("give mobile or account_number")
        rows = self.mb.query(self.db, f"SELECT TOP {sql.integer(limit)} {self.COLUMNS} FROM {self.table} "
                                      f"WHERE {' OR '.join(conditions)} ORDER BY date_added DESC")
        out = []
        for r in rows:
            row = {str(k).lower(): v for k, v in r.items()}
            same_account = bool(a and account_key(row.get("account_id")) and
                                str(account_key(row.get("account_id"))).endswith(a[-10:]))
            same_mobile = bool(m and mobile_key(row.get("msisdn")) == m)
            out.append({"linked_on": format_timestamp(parse_timestamp(row.get("date_added"))),
                        "recorded_by": trim_to_none(row.get("created_by")), "mobile": trim_to_none(row.get("msisdn")),
                        "account": trim_to_none(row.get("account_id")),
                        "currency_code": trim_to_none(row.get("currency_code")), "status": trim_to_none(row.get("status")),
                        "matches_mobile_searched": same_mobile if m else None,
                        "matches_account_searched": same_account if a else None})
        return out


# ---- core banking -------------------------------------------------------------------------------
IACLINK_SQL = "SELECT IACLINK_INTERNAL_ACNUM FROM NMBPRODIDC.IACLINK WHERE IACLINK_ACTUAL_ACNUM = '{account}'"
ACCOUNT_SQL = "SELECT ACNTS_CLIENT_NUM, ACNTS_CURR_CODE FROM NMBPRODIDC.ACNTS WHERE ACNTS_INTERNAL_ACNUM = '{internal}'"
CLIENT_MOBILE_SQL = ("SELECT C.INDCLIENT_TEL_RES FROM NMBPRODIDC.ACNTS A JOIN INDCLIENTS C "
                     "ON C.INDCLIENT_CODE = A.ACNTS_CLIENT_NUM WHERE A.ACNTS_INTERNAL_ACNUM = '{internal}'")


class BankAccounts:
    """Core banking through Metabase (the same tables the SMS agent reads in production).

    extra_sql: optional queries, keyed by a label, run with {internal} and {client} filled in,
    for details whose tables differ between installations (account status, ID on file)."""

    def __init__(self, metabase: MetabaseClient, database_id: int, extra_sql: Optional[dict[str, str]] = None,
                 pending_sql: Optional[str] = None) -> None:
        self.mb = metabase
        self.db = database_id
        self.extra_sql = extra_sql or {}
        self.pending_sql = (pending_sql or "").strip() or None

    def pending_changes(self, account: str, limit: int = 20) -> dict[str, Any]:
        """Profile changes captured at a branch and not yet authorised (PENDING_CHANGES_SQL)."""
        actual = normalize_account_number(account)
        if actual is None or self.pending_sql is None:
            raise ValueError("account_number must contain the account digits")
        rows = self.mb.query(self.db, IACLINK_SQL.format(account=sql.digits(actual)))
        if not rows:
            return {"account_number": actual, "exists": False}
        internal = str(_first(rows[0], "IACLINK_INTERNAL_ACNUM") or "")
        acct = self.mb.query(self.db, ACCOUNT_SQL.format(internal=sql.oracle_text(internal)))
        client = str(_first(acct[0], "ACNTS_CLIENT_NUM") or "") if acct else ""
        query = (self.pending_sql.replace("{account}", sql.digits(actual)).replace("{internal}", sql.oracle_text(internal))
                 .replace("{client}", sql.oracle_text(client)))
        found = self.mb.query(self.db, query)
        plain = [{str(k).lower(): (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in found]
        return {"account_number": actual, "exists": True, "pending_changes": plain[:limit], "truncated": len(plain) > limit}

    def check(self, account: str) -> dict[str, Any]:
        actual = normalize_account_number(account)
        if actual is None:
            raise ValueError("account_number must contain the account digits")
        rows = self.mb.query(self.db, IACLINK_SQL.format(account=sql.digits(actual)))
        if not rows:
            return {"account_number": actual, "exists": False}
        internal = str(_first(rows[0], "IACLINK_INTERNAL_ACNUM") or "")
        out: dict[str, Any] = {"account_number": actual, "exists": True}
        acct = self.mb.query(self.db, ACCOUNT_SQL.format(internal=sql.oracle_text(internal)))
        client = str(_first(acct[0], "ACNTS_CLIENT_NUM") or "") if acct else ""
        if acct:
            out["currency_code"] = trim_to_none(_first(acct[0], "ACNTS_CURR_CODE"))
        try:
            phone = self.mb.query(self.db, CLIENT_MOBILE_SQL.format(internal=sql.oracle_text(internal)))
            out["mobile_on_file"] = trim_to_none(_first(phone[0], "INDCLIENT_TEL_RES")) if phone else None
        except MetabaseError as exc:
            out["mobile_on_file"] = f"not available ({str(exc)[:80]})"
        for label, template in self.extra_sql.items():
            try:
                extra = self.mb.query(self.db, template.format(internal=sql.oracle_text(internal),
                                                               client=sql.oracle_text(client)))
                out[label] = [{str(k).lower(): v for k, v in r.items()} for r in extra[:3]]
            except (MetabaseError, KeyError, IndexError) as exc:
                out[label] = f"not available ({str(exc)[:80]})"
        return out


def _first(row: dict[str, Any], column: str) -> Any:
    for key, value in row.items():
        if str(key).upper() == column.upper():
            return value
    return None
