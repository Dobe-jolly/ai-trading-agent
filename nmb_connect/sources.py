"""Where an NMB Connect problem can be seen, through Metabase, read-only:

- CONECT 2.0 REG (PostgreSQL): the client's NMB Connect profile (table customer, key customer_number = CIF): whether
  it exists, how many there are, the mobile the OTPs go to, when it was created, and its state. Channels and login
  history are read too when those tables exist (user_channels, history).
- CONECT 2.0 (PostgreSQL) transactions: what the client did on the app or USSD lately and what came back (balance
  enquiries show they could log in; a failure shows the exact message).
- STANDBY DB (Oracle standby of core banking): the client's CIF, accounts and their state (dormant, closed), and
  the mobile and e-mail on the customer record. Never the live core banking system.
- registration_failures (PostgreSQL, the registration log analyzer): failed registrations, with the reason the
  analyzer gives them. Optional: only when Metabase has the database.
- SOCKET SMS DB (PostgreSQL) transaction_activity: the SMS the bank's gateway sent to a number and whether they were
  delivered. Optional. The text of a message is never returned, only what kind it was (OTP, alert, ...).

The profile table's columns are not documented, so rows are read whole and filtered here, in code: secrets
(passwords, PINs, OTPs, tokens, device and location data), names, birth dates and addresses never leave this
module. Only identifiers (compared in code, masked for the model), dates and state-like columns are returned.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    cif_key,
    email_key,
    format_timestamp,
    id_key,
    mobile_key,
    normalize_account_number,
    parse_timestamp,
    trim_to_none,
    truthy,
)

log = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# ---- which columns of an undocumented table may be shown --------------------------------------------------
# Always secret, whatever else the name says.
HARD_SECRET = re.compile(r"(?i)salt|secret|token|hash|cipher|encrypt|private|cookie|session|answer|question|imei|"
                         r"device|(?:^|_)mac(?:_|$)|(?:^|_)ip(?:_|$)|ip_?address|latitude|longitude|(?:^|_)lat$|"
                         r"(?:^|_)lon$|location|card|(?:^|_)pan(?:_|$)|cvv|signature|biometric|finger|face|photo|"
                         r"image|(?:^|_)key(?:_|$)|registration_code|activation_code|verification_code|security")
# Secret unless the column is a counter, flag or date about it (pin_tries, password_expired, otp_sent_at).
SOFT_SECRET = re.compile(r"(?i)pass|pwd|(?:^|_)pin|otp|user_?name|login_?name")
ABOUT_IT = re.compile(r"(?i)attempt|retr|tries|count|fail|lock|block|status|active|enabl|expir|chang|reset|"
                      r"_at$|_on$|date|time|sent|last")
PERSONAL = re.compile(r"(?i)(?:first|last|middle|full|sur|short|maiden|display)_?name|^name$|^names$|dob|birth|"
                      r"address|street|town|city|gender|^title$|nationality|marital|occupation|employer|kin")
STATE = re.compile(r"(?i)status|state|active|enabl|lock|block|suspend|delet|retired|remov|deactiv|attempt|retr|"
                   r"tries|fail|count|channel|type|level|role|last_|login|logged|updated|modified|changed|expir|"
                   r"registered|verified|activated|reset|limit|source|sent|_at$|_on$|date")
CIF_COLUMNS = ("customer_number", "cif", "cif_number", "customer_no", "client_number", "cif_no")
MOBILE_COLUMNS = ("mobile_number", "msisdn", "phone_number", "mobile", "phone", "cell_number", "cellphone")
EMAIL_COLUMNS = ("email", "email_address", "emailaddress")
CREATED_COLUMNS = ("created_at", "date_created", "created_date", "created", "creation_date", "registration_date",
                   "registered_at", "date_registered", "created_on", "createdon")
ROW_ID_COLUMNS = ("id", "customer_id", "uuid")
GONE = re.compile(r"(?i)delet|remov|closed|deactiv|retired|inactive|disabled")
LOCKED = re.compile(r"(?i)lock|block|suspend|frozen|freez")


def column_kind(name: str) -> str:
    """secret | personal | identifier | state | other (other: the name is shown, never the value)."""
    lowered = name.lower()
    if lowered in CIF_COLUMNS + MOBILE_COLUMNS + EMAIL_COLUMNS + ("account_number", "id_number", "national_id"):
        return "identifier"
    if HARD_SECRET.search(name):
        return "secret"
    if SOFT_SECRET.search(name) and not ABOUT_IT.search(name):
        return "secret"
    if PERSONAL.search(name):
        return "personal"
    if STATE.search(name) or lowered in CREATED_COLUMNS:
        return "state"
    return "other"


def _scalar(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, dict, list)):
        return None
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, str):
        stamp = parse_timestamp(value) if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d", value) else None
        return format_timestamp(stamp) if stamp else value[:120]
    return value


def _lower(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


# ---- CONECT 2.0 REG: the NMB Connect profile ---------------------------------------------------------------
@dataclass
class Profile:
    """One NMB Connect profile row, reduced to what may be shown."""
    ref: str                      # P1, P2, ... (this ticket's lookups; never a database key)
    row_id: Optional[str]         # the table's own id, for channels and history (never shown)
    cif: Optional[str]            # cif_key of customer_number
    mobile: Optional[str]         # as stored
    email: Optional[str]
    created: Optional[datetime]
    active: bool                  # False when a deleted / retired / inactive marker says so
    locked: bool                  # a locked / blocked / suspended marker
    state: dict = field(default_factory=dict)       # state-like columns: name -> value
    other_columns: list = field(default_factory=list)  # names only
    found_by: set = field(default_factory=set)      # {"cif", "mobile"}

    @property
    def mobile9(self) -> Optional[str]:
        return mobile_key(self.mobile)

    @classmethod
    def from_row(cls, ref: str, raw: dict[str, Any]) -> "Profile":
        row = _lower(raw)
        state: dict[str, Any] = {}
        other: list[str] = []
        active, locked = True, False
        for name, value in row.items():
            kind = column_kind(name)
            if kind == "state" and name not in CREATED_COLUMNS:
                state[name] = _scalar(value)
                flag = truthy(value)
                if GONE.search(name) and flag is True:
                    active = False
                if re.search(r"(?i)^(is_)?(active|enabled)$", name) and flag is False:
                    active = False
                if LOCKED.search(name) and flag is True:
                    locked = True
                if isinstance(value, str) and re.search(r"(?i)status|state", name):
                    if GONE.search(value):
                        active = False
                    elif LOCKED.search(value):
                        locked = True
            elif kind == "other" and name not in ROW_ID_COLUMNS:
                other.append(name)
        return cls(ref=ref, row_id=trim_to_none(_first(row, ROW_ID_COLUMNS)), cif=cif_key(_first(row, CIF_COLUMNS)),
                   mobile=trim_to_none(_first(row, MOBILE_COLUMNS)), email=trim_to_none(_first(row, EMAIL_COLUMNS)),
                   created=parse_timestamp(_first(row, CREATED_COLUMNS)), active=active, locked=locked,
                   state=state, other_columns=sorted(other))

    def same_row(self, other: "Profile") -> bool:
        if self.row_id and other.row_id:
            return self.row_id == other.row_id
        return (self.cif, self.mobile, self.created) == (other.cif, other.mobile, other.created)

    def describe(self, ticket_mobiles: set, client_cifs: set) -> dict[str, Any]:
        out: dict[str, Any] = {
            "profile": self.ref, "cif": self.cif, "mobile_on_profile": self.mobile,
            "mobile_matches_ticket": ("no mobile on the ticket" if not ticket_mobiles else
                                      "yes" if self.mobile9 in ticket_mobiles else "no"),
            "belongs_to_ticket_cif": ("unknown (no CIF known for the client)" if not client_cifs else
                                      "yes" if self.cif in client_cifs else "no"),
            "created": format_timestamp(self.created) or "unknown", "active": self.active,
            "locked_or_blocked": self.locked, "found_by": sorted(self.found_by),
        }
        if self.email:
            out["email_on_profile"] = self.email
        if self.state:
            out["state"] = self.state
        return out


class Registration:
    """CONECT 2.0 REG. The customer table is known (customer_number = CIF, mobile_number); channel and login
    history tables are optional and read when they exist."""

    def __init__(self, mb: MetabaseClient, db: int, customer_table: str = "customer",
                 channels_table: Optional[str] = "user_channels", history_table: Optional[str] = "history",
                 limit: int = 10) -> None:
        for name in (customer_table, channels_table, history_table):
            if name and not IDENTIFIER.fullmatch(name):
                raise ValueError(f"not a table name: {name!r}")
        self.mb, self.db, self.limit = mb, db, limit
        self.customer, self.channels_table, self.history_table = customer_table, channels_table, history_table

    def by_cif(self, cif: str) -> list[dict[str, Any]]:
        key = sql.digits(cif_key(cif) or "")
        return self.mb.query(self.db, f"SELECT * FROM {self.customer} "
                                      f"WHERE LTRIM(CAST(customer_number AS TEXT), '0') = '{key}' LIMIT {self.limit}")

    def by_mobile(self, mobile: str) -> list[dict[str, Any]]:
        key = sql.digits(mobile_key(mobile) or "")
        return self.mb.query(self.db, f"SELECT * FROM {self.customer} WHERE RIGHT(REGEXP_REPLACE(CAST(mobile_number "
                                      f"AS TEXT), '[^0-9]', '', 'g'), 9) = '{key}' LIMIT {self.limit}")

    def _for_ids(self, table: Optional[str], ids: list[str], order: str = "") -> list[dict[str, Any]]:
        if not table or not ids:
            return []
        values = ", ".join(f"'{sql.oracle_text(i)}'" for i in ids[:5])
        return self.mb.query(self.db, f"SELECT * FROM {table} WHERE CAST(customer_id AS TEXT) IN ({values})"
                                      f"{order} LIMIT 20")

    def channels(self, ids: list[str]) -> list[dict[str, Any]]:
        return [_safe_row(r) for r in self._for_ids(self.channels_table, ids)]

    def history(self, ids: list[str]) -> list[dict[str, Any]]:
        return [_safe_row(r) for r in self._for_ids(self.history_table, ids, " ORDER BY created_at DESC")][:8]

    def schema(self) -> dict[str, list[str]]:
        """Tables and, for the ones this desk reads, their column names (no data): for `check`."""
        tables = [r.get("table_name") for r in self.mb.query(self.db, (
            "SELECT table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', "
            "'information_schema') ORDER BY table_name"))]
        wanted = [t for t in (self.customer, self.channels_table, self.history_table) if t]
        names = ", ".join(f"'{sql.oracle_text(t.split('.')[-1])}'" for t in wanted)
        columns: dict[str, list[str]] = {"_tables": [str(t) for t in tables]}
        for row in self.mb.query(self.db, f"SELECT table_name, column_name FROM information_schema.columns WHERE "
                                          f"table_name IN ({names}) ORDER BY table_name, ordinal_position"):
            columns.setdefault(str(row.get("table_name")), []).append(str(row.get("column_name")))
        return columns


def _safe_row(raw: dict[str, Any]) -> dict[str, Any]:
    """A channel or history row: state-like columns and dates only."""
    out = {}
    for name, value in _lower(raw).items():
        kind = column_kind(name)
        if kind == "state" or name in CREATED_COLUMNS:
            out[name] = _scalar(value)
    return out


# ---- CONECT 2.0 transactions: what the client did on the app or USSD -------------------------------------------
class AppActivity:
    def __init__(self, mb: MetabaseClient, db: int, limit: int = 40) -> None:
        self.mb, self.db, self.limit = mb, db, limit

    def recent(self, cif: Optional[str], mobile: Optional[str], since: date) -> list[dict[str, Any]]:
        where = []
        if cif:
            where.append(f"LTRIM(CAST(customer_number AS TEXT), '0') = '{sql.digits(cif_key(cif) or '')}'")
        if mobile:
            where.append(f"RIGHT(REGEXP_REPLACE(CAST(customer_mobile AS TEXT), '[^0-9]', '', 'g'), 9) = "
                         f"'{sql.digits(mobile_key(mobile) or '')}'")
        if not where:
            raise ValueError("give a CIF or a mobile number")
        rows = self.mb.query(self.db, (
            "SELECT created_date, channel, operation, transaction_type, status, response_code, response_message "
            f"FROM transactions WHERE ({' OR '.join(where)}) AND created_date >= '{sql.iso_date(since)}' "
            f"ORDER BY created_date DESC LIMIT {int(self.limit)}"))
        out = []
        for raw in rows:
            row = _lower(raw)
            out.append({"when": parse_timestamp(row.get("created_date")), "channel": trim_to_none(row.get("channel")),
                        "operation": trim_to_none(row.get("operation")) or trim_to_none(row.get("transaction_type")),
                        "status": (trim_to_none(row.get("status")) or "").upper() or None,
                        "code": trim_to_none(row.get("response_code")),
                        "message": (trim_to_none(row.get("response_message")) or "")[:160] or None})
        return out


def summarise_activity(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    if not rows:
        return {"found": 0, "summary": f"No app or USSD activity in the last {days} days."}
    by_status: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    for r in rows:
        by_status[r["status"] or "unknown"] = by_status.get(r["status"] or "unknown", 0) + 1
        by_channel[r["channel"] or "unknown"] = by_channel.get(r["channel"] or "unknown", 0) + 1
    ok = next((r for r in rows if r["status"] == "SUCCESS"), None)
    bad = next((r for r in rows if r["status"] and r["status"] != "SUCCESS"), None)

    def brief(r: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if r is None:
            return None
        out = {"when": format_timestamp(r["when"]), "channel": r["channel"], "operation": r["operation"],
               "status": r["status"]}
        if r["status"] != "SUCCESS":
            out.update(code=r["code"], message=r["message"])
        return out

    return {"found": len(rows), "by_status": by_status, "by_channel": by_channel, "last_success": brief(ok),
            "last_failure": brief(bad), "recent": [brief(r) for r in rows[:10]],
            "summary": (f"{len(rows)} app/USSD request(s) in the last {days} days; last success "
                        f"{format_timestamp(ok['when']) if ok else 'none'}")}


# ---- core banking (standby): the client's CIF, accounts and contact details ----------------------------------
IACLINK_SQL = "SELECT IACLINK_INTERNAL_ACNUM FROM NMBPRODIDC.IACLINK WHERE IACLINK_ACTUAL_ACNUM = '{account}'"
ACNTS_SQL = "SELECT * FROM NMBPRODIDC.ACNTS WHERE ACNTS_INTERNAL_ACNUM = '{internal}'"
CIF_ACCOUNTS_SQL = """SELECT X.IACLINK_ACTUAL_ACNUM, A.*
FROM NMBPRODIDC.ACNTS A
JOIN NMBPRODIDC.IACLINK X ON X.IACLINK_INTERNAL_ACNUM = A.ACNTS_INTERNAL_ACNUM
WHERE A.ACNTS_CLIENT_NUM = {cif} AND ROWNUM <= 25"""
CLIENT_SQL = "SELECT INDCLIENT_TEL_RES, INDCLIENT_EMAIL_ADDR1 FROM INDCLIENTS WHERE INDCLIENT_CODE = {cif}"
IDCCOB_SQL = """SELECT A.COBICADDR_MOB_NO
FROM COBICINIT I
JOIN COBICADDR A ON A.COBICADDR_CREF_NO = I.COBICINIT_CREF_NO
WHERE I.COBICINIT_CIF_NO = '{cif}'"""
INACTIVE_STATES = ("closed", "dormant", "inoperative", "frozen")


def account_state(row: dict[str, Any]) -> tuple[str, list[str]]:
    """(closed | dormant | inoperative | frozen | active | unknown, the columns that said so) from an ACNTS row.
    Intellect's column names differ between versions, so any flag or closure date with a telling name counts."""
    seen: dict[str, list[str]] = {s: [] for s in INACTIVE_STATES}
    considered = False
    for name, value in row.items():
        upper = str(name).upper()
        if not upper.startswith("ACNTS_"):
            continue
        parts = upper.split("_")
        if ("CLOSURE" in upper or "CLOSED" in upper) and ("DATE" in upper or parts[-1] in ("DT", "ON")):
            considered = True
            if value not in (None, ""):
                seen["closed"].append(upper)
            continue
        if any(word in upper for word in ("DAYS", "DATE", "AMT", "BAL", "COUNT", "NUM")) or parts[-1] == "DT":
            continue  # counts, amounts and dates are not flags
        flag = truthy(value)
        for state, markers in (("closed", ("CLOSED",)), ("dormant", ("DORM",)), ("inoperative", ("INOP",)),
                               ("frozen", ("FREEZ", "FROZEN"))):
            if any(m in upper for m in markers) and flag is not None:
                considered = True
                if flag:
                    seen[state].append(upper)
    for state in INACTIVE_STATES:
        if seen[state]:
            return state, seen[state]
    return ("active" if considered else "unknown"), []


@dataclass
class CoreAccount:
    account: str                  # 14-digit form
    exists: bool
    cif: Optional[str] = None
    currency: Optional[str] = None
    state: str = "unknown"
    state_from: list = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        if not self.exists:
            return {"account_number": self.account, "exists": False}
        out = {"account_number": self.account, "exists": True, "cif": self.cif, "currency": self.currency,
               "state": self.state}
        if self.state_from:
            out["state_from"] = self.state_from
        return out


class CoreBanking:
    def __init__(self, mb: MetabaseClient, db: int, pending_sql: Optional[str] = None) -> None:
        self.mb, self.db = mb, db
        self.pending_sql = (pending_sql or "").strip() or None

    def _first(self, query: str) -> Optional[dict[str, Any]]:
        rows = self.mb.query(self.db, query)
        return rows[0] if rows else None

    def account(self, account: str) -> CoreAccount:
        actual = normalize_account_number(account)
        if actual is None:
            raise ValueError("account_number must contain the account digits")
        link = self._first(IACLINK_SQL.format(account=sql.digits(actual)))
        if link is None:
            return CoreAccount(account=actual, exists=False)
        internal = str(_first({str(k).upper(): v for k, v in link.items()}, ("IACLINK_INTERNAL_ACNUM",)) or "")
        row = self._first(ACNTS_SQL.format(internal=sql.oracle_text(internal))) or {}
        upper = {str(k).upper(): v for k, v in row.items()}
        state, state_from = account_state(upper)
        return CoreAccount(account=actual, exists=True, cif=cif_key(upper.get("ACNTS_CLIENT_NUM")),
                           currency=trim_to_none(upper.get("ACNTS_CURR_CODE")), state=state, state_from=state_from)

    def customer(self, cif: str) -> dict[str, Any]:
        key = sql.digits(cif_key(cif) or "")
        accounts = []
        for raw in self.mb.query(self.db, CIF_ACCOUNTS_SQL.format(cif=key)):
            upper = {str(k).upper(): v for k, v in raw.items()}
            state, state_from = account_state(upper)
            accounts.append(CoreAccount(account=normalize_account_number(upper.get("IACLINK_ACTUAL_ACNUM")) or "",
                                        exists=True, cif=key, currency=trim_to_none(upper.get("ACNTS_CURR_CODE")),
                                        state=state, state_from=state_from))
        out: dict[str, Any] = {"cif": key, "exists": bool(accounts), "accounts": accounts}
        try:
            client = self._first(CLIENT_SQL.format(cif=key)) or {}
            upper = {str(k).upper(): v for k, v in client.items()}
            out["mobile_on_file"] = trim_to_none(upper.get("INDCLIENT_TEL_RES"))
            out["email_on_file"] = email_key(upper.get("INDCLIENT_EMAIL_ADDR1"))
        except MetabaseError as exc:
            out["contact_error"] = str(exc)[:120]
        try:
            rows = self.mb.query(self.db, IDCCOB_SQL.format(cif=key))
            out["idccob_mobiles"] = sorted({m for m in (trim_to_none(_first({str(k).upper(): v for k, v in r.items()},
                                                                             ("COBICADDR_MOB_NO",))) for r in rows) if m})
        except MetabaseError as exc:
            out["idccob_error"] = str(exc)[:120]
        return out

    def pending_changes(self, cif: str, account: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Profile changes captured at a branch and not yet authorised (PENDING_CHANGES_SQL), for the CIF (and the
        account, when the query selects by account)."""
        if self.pending_sql is None:
            raise ValueError("pending changes are not configured")
        actual = normalize_account_number(account) if account else None
        internal = ""
        if actual and "{internal}" in self.pending_sql:
            link = self._first(IACLINK_SQL.format(account=sql.digits(actual))) or {}
            internal = str(_first({str(k).upper(): v for k, v in link.items()}, ("IACLINK_INTERNAL_ACNUM",)) or "")
        if ("{account}" in self.pending_sql or "{internal}" in self.pending_sql) and not actual:
            raise ValueError("pending changes are looked up by account here: give account_number")
        query = (self.pending_sql.replace("{client}", sql.oracle_text(sql.digits(cif_key(cif) or "")))
                 .replace("{account}", sql.digits(actual) if actual else "")
                 .replace("{internal}", sql.oracle_text(internal)))
        rows = self.mb.query(self.db, query)
        return [{str(k).lower(): _scalar(v) for k, v in r.items()} for r in rows[:limit]]


# ---- the registration log analyzer: failed registrations and why ----------------------------------------------
# Ported from the analyzer's diagnosis rules (application.yml), first match wins.
FAILURE_RULES = [
    ("DUPLICATE_PROFILE", re.compile(r"NonUniqueResultException", re.I),
     "more than one customer profile exists for this client in the registration database"),
    ("CREDENTIAL_VALIDATION_FAILED", re.compile(r"(EMAIL|USERNAME)/PASSWORD REQUEST FAILED", re.I),
     "the e-mail and password entered did not match the client's records"),
    ("DB_QUERY_ERROR", re.compile(r"SQLException|DataAccessException|JDBCConnectionException|QueryTimeoutException", re.I),
     "a database error while processing the registration (temporary)"),
    ("UPSTREAM_TIMEOUT", re.compile(r"SocketTimeoutException|ConnectException|ResourceAccessException|TimeoutException", re.I),
     "a core banking service was unreachable or timed out (temporary)"),
]


def failure_reason(text: str) -> tuple[str, str]:
    for code, pattern, explanation in FAILURE_RULES:
        if pattern.search(text or ""):
            return code, explanation
    return "UNKNOWN", "the failure does not match a known pattern"


class RegistrationFailures:
    def __init__(self, mb: MetabaseClient, db: int, limit: int = 10) -> None:
        self.mb, self.db, self.limit = mb, db, limit

    def find(self, mobile: Optional[str], id_number: Optional[str]) -> list[dict[str, Any]]:
        where = []
        if mobile:
            where.append("RIGHT(REGEXP_REPLACE(CAST(mobile_number AS TEXT), '[^0-9]', '', 'g'), 9) = "
                         f"'{sql.digits(mobile_key(mobile) or '')}'")
        if id_number:
            key = id_key(id_number)
            if not key or not re.fullmatch(r"[0-9A-Z]{8,14}", key):
                raise ValueError("id_number is not a national ID")
            where.append(f"id_number_normalized = '{key}'")
        if not where:
            raise ValueError("give a mobile number or a national ID")
        rows = self.mb.query(self.db, (
            "SELECT log_timestamp, failure_type, error_message, root_cause, channel FROM failed_registration "
            f"WHERE {' OR '.join(where)} ORDER BY log_timestamp DESC LIMIT {int(self.limit)}"))
        out = []
        for raw in rows:
            row = _lower(raw)
            text = f"{row.get('root_cause') or ''} {row.get('error_message') or ''}"
            code, explanation = failure_reason(text)
            out.append({"when": parse_timestamp(row.get("log_timestamp")), "reason_code": code,
                        "explanation": explanation, "channel": trim_to_none(row.get("channel")),
                        "message": (trim_to_none(row.get("error_message")) or "")[:160] or None})
        return out


# ---- the SMS gateway: what was sent to a number ----------------------------------------------------------------
DELIVERED, PENDING, FAILED, UNKNOWN = "delivered", "pending", "failed", "unknown"
OTP_WORDS = re.compile(r"(?i)\b(otp|one[- ]time|verification code|activation code|security code|code is|"
                       r"passcode)\b")


def gateway_status(external: Optional[str]) -> str:
    value = (external or "").strip().upper()
    if value == "DELIVERED_SUCCESSFULLY":
        return DELIVERED
    if value == "SENT_AWAITING_DELIVERY":
        return PENDING
    if value in ("FAILED_TO_SEND", "FAILED_TO_DELIVER"):
        return FAILED
    return UNKNOWN


def message_kind(text: str) -> str:
    """otp | username | alert | other. Only the kind leaves this module, never the text (it holds the codes)."""
    if OTP_WORDS.search(text or ""):
        return "otp"
    if re.search(r"(?i)user\s?name", text or ""):
        return "username"
    if re.search(r"(?i)\b(USD|ZWG|ZIG)\s?\d|debit|credit", text or ""):
        return "alert"
    return "other"


class SmsGateway:
    def __init__(self, mb: MetabaseClient, db: int, limit: int = 30) -> None:
        self.mb, self.db, self.limit = mb, db, limit

    def sent_to(self, mobile: str, since: date) -> list[dict[str, Any]]:
        key = sql.digits(mobile_key(mobile) or "")
        rows = self.mb.query(self.db, (
            "SELECT time_stamp, service_provider_name, external_status, logged_responses\n"
            "FROM public.transaction_activity\n"
            "WHERE service_type = 'VALUE_ADDED_SERVICES' AND service_operation_type = 'SEND_SMS'\n"
            f"  AND time_stamp >= TIMESTAMP '{sql.iso_date(since)} 00:00:00'\n"
            f"  AND logged_responses LIKE '%{key}%'\n"
            f"ORDER BY time_stamp DESC\nLIMIT {int(self.limit)}"))
        return [{"when": parse_timestamp(r.get("time_stamp")), "network": trim_to_none(r.get("service_provider_name")),
                 "status": gateway_status(r.get("external_status")), "kind": message_kind(str(r.get("logged_responses") or ""))}
                for r in rows]


def summarise_sms(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    otps = [r for r in rows if r["kind"] == "otp"]
    counts: dict[str, int] = {}
    for r in otps:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    last = otps[0] if otps else None
    return {
        "messages": len(rows), "otp_messages": len(otps), "otp_by_status": counts,
        "last_otp": {"when": format_timestamp(last["when"]), "status": last["status"], "network": last["network"]}
        if last else None,
        "recent": [{"when": format_timestamp(r["when"]), "kind": r["kind"], "status": r["status"]} for r in rows[:10]],
        "summary": (f"{len(rows)} SMS in the last {days} days, {len(otps)} of them OTPs"
                    + (f" (last {format_timestamp(last['when'])}: {last['status']})" if last else "")),
    }


def otp_failing(summary: Optional[dict[str, Any]]) -> bool:
    """OTPs were sent and none was delivered: a gateway or network problem, not the handset."""
    if not summary or not summary.get("otp_messages"):
        return False
    counts = summary.get("otp_by_status") or {}
    return counts.get(FAILED, 0) > 0 and counts.get(DELIVERED, 0) == 0


def window_start(today: date, days: int) -> date:
    return today - timedelta(days=days)
