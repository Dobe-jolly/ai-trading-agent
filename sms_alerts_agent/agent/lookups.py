"""Read-only lookups the model can use to investigate beyond the standard check.

Each one is fixed SQL (the same tables the Java check already reads), scoped to a number that is on
the ticket or came back from an earlier result, and its result is masked before the model sees it:

- account_activity: every transaction on an account between two dates, whether it raised an alert,
  and what the SMS gateway did with those alerts ("I withdrew on the 3rd and got no SMS").
- gateway_messages: every SMS the gateway sent to a number between two dates, with its status.
- customer_profile: the numbers, registrations and e-mail on file for an account, in one place.
- pending_profile_changes: changes captured at a branch but not yet authorised. The query comes from
  PENDING_CHANGES_SQL (Intellect's maker-checker tables); the tool is hidden until it is set.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from agentcore import sql
from .delivery import AlertTrigger, SocketDeliveryChecker, status_from_external
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    format_timestamp,
    is_sms_activated,
    is_usable_customer_email,
    normalize_account_number,
    normalize_gateway_phone,
    parse_timestamp,
    subscriber,
    today_local,
    trim_to_none,
)
from .sms_checks import (
    ACNTS_ALERT_PHONE_SQL,
    ACNTS_CURRENCY_SQL,
    ACNTS_SMS_SQL,
    COBICADDR_SQL,
    Databases,
    SmsChecks,
    _first,
)

log = logging.getLogger(__name__)

MAX_RANGE_DAYS = 31
OLDEST_DAYS = 400
MAX_ACTIVITY_ROWS = 60
MAX_MESSAGES = 40
MAX_PENDING_ROWS = 20

# Every transaction on the account, with its alert event when one was raised (the Java's trigger query
# with the alert tables outer-joined).
ACTIVITY_SQL = """SELECT * FROM (
SELECT
    S.TRAN_DATE_OF_TRAN,
    S.TRAN_CURR_CODE,
    S.TRAN_AMOUNT,
    K.UPTD_ON,
    Y.CMNAEC_EVENT_NAME,
    X.IACLINK_ACTUAL_ACNUM,
    INDCLIENTS.INDCLIENT_TEL_RES
FROM {tran_table} S
JOIN NMBPRODIDC.IACLINK X
    ON X.IACLINK_ENTITY_NUM = S.TRAN_ENTITY_NUM
   AND X.IACLINK_INTERNAL_ACNUM = S.TRAN_INTERNAL_ACNUM
LEFT JOIN NMBPRODIDC.EVTSLOG K
    ON S.TRAN_ENTITY_NUM = K.ENTITY_CODE
   AND (S.TRAN_ENTITY_NUM || '|' || S.TRAN_BRN_CODE || '|' ||
        TO_CHAR(S.TRAN_DATE_OF_TRAN,'DD-MM-YYYY') || '|' ||
        S.TRAN_BATCH_NUMBER || '|' || S.TRAN_BATCH_SL_NUM || '|') = K.SOURCE_KEY
LEFT JOIN NMBPRODIDC.CMNALERTEVENTCFG Y
    ON Y.CMNAEC_ENTITY_NUM = S.TRAN_ENTITY_NUM
   AND Y.CMNAEC_EVENT_CODE = K.EVENT_CODE
   AND Y.CMNAEC_EVENT_ENABLED = '1'
LEFT JOIN INDCLIENTS
    ON S.TRAN_PROFIT_CUST_CODE = INDCLIENTS.INDCLIENT_CODE
WHERE S.TRAN_ENTITY_NUM = 1
  AND X.IACLINK_ACTUAL_ACNUM = '{account_number}'
  AND S.TRAN_DATE_OF_TRAN BETWEEN TO_DATE('{start_date}','DD-MM-YYYY')
                              AND TO_DATE('{end_date}','DD-MM-YYYY')
ORDER BY S.TRAN_DATE_OF_TRAN DESC
) WHERE ROWNUM <= {limit}"""
CLIENT_NUM_SQL = "SELECT ACNTS_CLIENT_NUM\nFROM NMBPRODIDC.ACNTS\nWHERE ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"
AMOUNT_RE = re.compile(r"\b(USD|ZWG|ZIG|ZWL|ZAR)\s?([\d,]+\.\d{2})", re.I)
KIND_WORDS = (("debit", "debit"), ("withdraw", "debit"), ("credit", "credit"), ("deposit", "credit"),
              ("salary", "credit"))

ACTIVITY_TOOL: dict[str, Any] = {
    "name": "get_account_activity",
    "description": (
        "Every transaction on ONE account between two dates (at most 31 days, within the last 13 months), whether "
        "each raised an SMS alert in core banking, and what the SMS gateway did with the alerts. Use it when the "
        "complaint is about specific dates or transactions, or when the standard check's 15-day window does not "
        "cover them. A transaction with no alert event means core banking never raised an alert for it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_number": {"type": "string", "description": "A masked account from the ticket or a result, e.g. <NUMBER_1>."},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["account_number", "date_from", "date_to"],
    },
}
MESSAGES_TOOL: dict[str, Any] = {
    "name": "get_gateway_messages",
    "description": (
        "Every SMS the gateway sent to ONE mobile number between two dates (at most 31 days): when, which network, "
        "the delivery status, and the amount and kind of alert where the message shows them. Use it to see which "
        "alerts actually reached a number (late, missing, or going to an old number)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mobile": {"type": "string", "description": "A masked mobile from the ticket or a result, e.g. <MOBILE_1>."},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["mobile", "date_from", "date_to"],
    },
}
PROFILE_TOOL: dict[str, Any] = {
    "name": "get_customer_profile",
    "description": (
        "What core banking holds for ONE account's customer: the account currency and SMS flag, every IDCCOB "
        "alert registration (number and on/off), the mobile on the customer record, whether a usable e-mail is on "
        "file, and the salaries number. Use it to compare numbers across records, e.g. when the client says the "
        "number was changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"account_number": {"type": "string", "description": "A masked account, e.g. <NUMBER_1>."}},
        "required": ["account_number"],
    },
}
PENDING_TOOL: dict[str, Any] = {
    "name": "get_pending_profile_changes",
    "description": (
        "Changes to ONE account's customer profile captured at a branch but not yet authorised (for example a new "
        "mobile number awaiting approval). Use it when the thread says the number or details were recently changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"account_number": {"type": "string", "description": "A masked account, e.g. <NUMBER_1>."}},
        "required": ["account_number"],
    },
}


def parse_range(date_from: Any, date_to: Any, today: Optional[date] = None) -> tuple[date, date]:
    today = today or today_local()
    try:
        start = date.fromisoformat(str(date_from).strip()[:10])
        end = date.fromisoformat(str(date_to).strip()[:10])
    except ValueError:
        raise ValueError("dates must be YYYY-MM-DD") from None
    if end < start:
        start, end = end, start
    end = min(end, today)
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"the range can be at most {MAX_RANGE_DAYS} days")
    if start < today - timedelta(days=OLDEST_DAYS):
        raise ValueError("dates must be within the last 13 months")
    return start, end


def message_facts(text: str) -> dict[str, Any]:
    """Only what correlates a message with a transaction: amount, currency and kind. Never the raw text."""
    facts: dict[str, Any] = {}
    match = AMOUNT_RE.search(text or "")
    if match:
        facts["currency"] = match.group(1).upper()
        facts["amount"] = match.group(2).replace(",", "")
    lowered = (text or "").lower()
    for word, kind in KIND_WORDS:
        if word in lowered:
            facts["kind"] = kind
            break
    return facts


class Lookups:
    def __init__(self, metabase: MetabaseClient, checks: SmsChecks, pending_sql: Optional[str] = None,
                 pending_db: Optional[int] = None) -> None:
        self.mb = metabase
        self.checks = checks
        self.dbs: Databases = checks.dbs
        self.socket: SocketDeliveryChecker = checks.socket
        self.pending_sql = (pending_sql or "").strip() or None
        self.pending_db = pending_db if pending_db is not None else checks.dbs.standby

    def schemas(self) -> list[dict[str, Any]]:
        tools = [ACTIVITY_TOOL, PROFILE_TOOL]
        if self.dbs.socket is not None:
            tools.append(MESSAGES_TOOL)
        if self.pending_sql:
            tools.append(PENDING_TOOL)
        return tools

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.schemas()}

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Arguments arrive unmasked. Raises ValueError for bad input, MetabaseError for data-source failures."""
        if name == "get_account_activity":
            start, end = parse_range(args.get("date_from"), args.get("date_to"))
            return self.account_activity(_account(args.get("account_number")), start, end)
        if name == "get_gateway_messages":
            start, end = parse_range(args.get("date_from"), args.get("date_to"))
            return self.gateway_messages(str(args.get("mobile") or ""), start, end)
        if name == "get_customer_profile":
            return self.customer_profile(_account(args.get("account_number")))
        if name == "get_pending_profile_changes" and self.pending_sql:
            return self.pending_profile_changes(_account(args.get("account_number")))
        raise ValueError(f"unknown lookup {name!r}")

    # ---- the lookups --------------------------------------------------------------------
    def account_activity(self, account: str, start: date, end: date) -> dict[str, Any]:
        internal = self.checks.internal_account_number(account)
        if internal is None:
            return {"account_number": account, "found": False}
        rows: list[dict[str, Any]] = []
        for year in range(end.year, start.year - 1, -1):
            lo, hi = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            query = (ACTIVITY_SQL.replace("{tran_table}", self.checks.tran_table(year))
                     .replace("{account_number}", sql.oracle_text(sql.digits(account)))
                     .replace("{start_date}", sql.oracle_date(lo)).replace("{end_date}", sql.oracle_date(hi))
                     .replace("{limit}", str(MAX_ACTIVITY_ROWS)))
            try:
                rows += self.mb.query(self.dbs.standby, query)
            except MetabaseError as exc:
                if "ORA-00942" not in str(exc) or year == end.year == start.year:
                    raise
                log.warning("%s not available (%s)", self.checks.tran_table(year), exc)
        transactions = []
        triggers = []
        for r in rows[:MAX_ACTIVITY_ROWS]:
            raised = parse_timestamp(r.get("UPTD_ON")) if r.get("UPTD_ON") else None
            when = parse_timestamp(r.get("TRAN_DATE_OF_TRAN"))
            transactions.append({
                "date": when.date().isoformat() if when else str(r.get("TRAN_DATE_OF_TRAN") or "")[:10],
                "currency": r.get("TRAN_CURR_CODE"), "amount": r.get("TRAN_AMOUNT"),
                "alert_event": r.get("CMNAEC_EVENT_NAME") if raised else None,
                "alert_raised_at": format_timestamp(raised) if raised else None,
                "alert_addressed_to": trim_to_none(r.get("INDCLIENT_TEL_RES")) if raised else None,
            })
            if raised and r.get("CMNAEC_EVENT_NAME"):
                triggers.append(AlertTrigger.from_standby_row(r))
        gateway = None
        if triggers and self.dbs.socket is not None:
            gateway = self.socket.check(self.dbs.socket, triggers).to_dict()
        alerted = sum(1 for t in transactions if t["alert_event"])
        return {
            "account_number": account, "found": True, "range": f"{start.isoformat()} .. {end.isoformat()}",
            "transactions": transactions, "truncated": len(rows) >= MAX_ACTIVITY_ROWS,
            "summary": f"{len(transactions)} transaction(s), {alerted} raised an alert, {len(transactions) - alerted} did not",
            "gateway_for_alerts": gateway,
        }

    def gateway_messages(self, mobile: str, start: date, end: date) -> dict[str, Any]:
        if self.dbs.socket is None:
            raise MetabaseError("the SMS gateway database is not available")
        sub = subscriber(normalize_gateway_phone(mobile) or mobile)
        if sub is None:
            raise ValueError("not a Zimbabwean mobile number")
        sub = sql.digits(sub)
        query = ("SELECT time_stamp, service_provider_name, external_status, logged_responses\n"
                 "FROM public.transaction_activity\n"
                 "WHERE service_type = 'VALUE_ADDED_SERVICES' AND service_operation_type = 'SEND_SMS'\n"
                 f"  AND time_stamp >= TIMESTAMP '{start.isoformat()} 00:00:00'\n"
                 f"  AND time_stamp < TIMESTAMP '{(end + timedelta(days=1)).isoformat()} 00:00:00'\n"
                 f"  AND logged_responses LIKE '%{sub}%'\n"
                 f"ORDER BY time_stamp DESC\nLIMIT {MAX_MESSAGES + 1}")
        rows = self.mb.query(self.dbs.socket, query)
        messages = [{"at": format_timestamp(parse_timestamp(r.get("time_stamp"))), "network": r.get("service_provider_name"),
                     "status": status_from_external(r.get("external_status")),
                     **message_facts(str(r.get("logged_responses") or ""))} for r in rows[:MAX_MESSAGES]]
        counts: dict[str, int] = {}
        for m in messages:
            counts[m["status"]] = counts.get(m["status"], 0) + 1
        return {"mobile": mobile, "range": f"{start.isoformat()} .. {end.isoformat()}", "messages": messages,
                "by_status": counts, "truncated": len(rows) > MAX_MESSAGES}

    def customer_profile(self, account: str) -> dict[str, Any]:
        internal = self.checks.internal_account_number(account)
        if internal is None:
            return {"account_number": account, "found": False}
        q = self.checks._q
        lit = sql.oracle_text(internal)
        registrations = [{"mobile": trim_to_none(r.get("COBICADDR_MOB_NO")),
                          "sms_alert": str(r.get("COBICADDR_SMS_ALERT") or "").strip() or None}
                         for r in q(COBICADDR_SQL, internal_acnum=lit)]
        email_ok, email = self.checks.customer_email(internal)
        profile: dict[str, Any] = {
            "account_number": account, "found": True,
            "currency": _first(q(ACNTS_CURRENCY_SQL, internal_acnum=lit), "ACNTS_CURR_CODE"),
            "sms_flag_on": is_sms_activated(_first(q(ACNTS_SMS_SQL, internal_acnum=lit), "ACNTS_SMS_OPERN")),
            "alert_registrations": registrations,
            "customer_record_mobile": _first(q(ACNTS_ALERT_PHONE_SQL, internal_acnum=lit), "INDCLIENT_TEL_RES"),
            "usable_email_on_file": is_usable_customer_email(email) if email_ok else None,
        }
        if self.dbs.customers is not None:
            try:
                row = self.checks.salaries_account(account)
                profile["salaries_mobile"] = trim_to_none(row.get("cellphone")) if row else None
            except MetabaseError as exc:
                profile["salaries_mobile"] = f"lookup failed: {exc}"[:200]
        return profile

    def pending_profile_changes(self, account: str) -> dict[str, Any]:
        internal = self.checks.internal_account_number(account)
        if internal is None:
            return {"account_number": account, "found": False}
        client = _first(self.checks._q(CLIENT_NUM_SQL, internal_acnum=sql.oracle_text(internal)), "ACNTS_CLIENT_NUM")
        query = (self.pending_sql.replace("{account}", sql.oracle_text(sql.digits(account)))
                 .replace("{internal}", sql.oracle_text(internal))
                 .replace("{client}", sql.oracle_text(client or "")))
        rows = self.mb.query(self.pending_db, query)
        return {"account_number": account, "found": True, "pending_changes": [_plain(r) for r in rows[:MAX_PENDING_ROWS]],
                "truncated": len(rows) > MAX_PENDING_ROWS}


def _account(value: Any) -> str:
    account = normalize_account_number(value)
    if account is None or len(account) > 20:
        raise ValueError("account_number must be an account from the ticket")
    return account


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in row.items()}
