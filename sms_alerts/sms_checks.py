"""The SMS_ALERTS checks, ported from SmsAgentService (per-account verdict) and
AlertDeliveryInvestigator (pipeline trace).

Status vocabulary, SQL and reply wording are the Java's, so results are directly comparable
with what the Java agent posts today.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from agentcore import sql
from .delivery import DELIVERED, FAILED, NOT_FOUND, PENDING, AlertTrigger, SocketDeliveryChecker
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    ALERT_DEBITS,
    ALERT_SALARIES,
    format_timestamp,
    is_deliverable_phone,
    is_sms_activated,
    is_usable_customer_email,
    is_valid_ticket_phone,
    normalize_gateway_phone,
    normalize_ticket_phone,
    parse_alert_flag,
    parse_timestamp,
    subscriber,
    today_local,
    trim_to_none,
)

log = logging.getLogger(__name__)

# Statuses that must never auto-close (SmsAgentService.ESCALATING_STATUSES).
ESCALATING_STATUSES = frozenset(
    {"socket_pending", "socket_failed", "socket_not_found", "socket_unknown", "salaries_unverified"}
)

STANDBY_SQL = """SELECT
    K.UPTD_ON,
    K.EVENT_CODE,
    Y.CMNAEC_EVENT_NAME,
    X.IACLINK_ACTUAL_ACNUM,
    S.TRAN_CURR_CODE,
    S.TRAN_AMOUNT,
    Z.CLIENTS_CODE,
    INDCLIENTS.INDCLIENT_TEL_RES
FROM {tran_table} S
JOIN NMBPRODIDC.EVTSLOG K
    ON S.TRAN_ENTITY_NUM = K.ENTITY_CODE
   AND (S.TRAN_ENTITY_NUM || '|' || S.TRAN_BRN_CODE || '|' ||
        TO_CHAR(S.TRAN_DATE_OF_TRAN,'DD-MM-YYYY') || '|' ||
        S.TRAN_BATCH_NUMBER || '|' || S.TRAN_BATCH_SL_NUM || '|') = K.SOURCE_KEY
JOIN NMBPRODIDC.IACLINK X
    ON X.IACLINK_ENTITY_NUM = S.TRAN_ENTITY_NUM
   AND X.IACLINK_INTERNAL_ACNUM = S.TRAN_INTERNAL_ACNUM
JOIN NMBPRODIDC.CMNALERTEVENTCFG Y
    ON Y.CMNAEC_ENTITY_NUM = S.TRAN_ENTITY_NUM
   AND Y.CMNAEC_EVENT_CODE = K.EVENT_CODE
JOIN NMBPRODIDC.CLIENTS Z
    ON Z.CLIENTS_CODE = S.TRAN_PROFIT_CUST_CODE
JOIN INDCLIENTS
    ON S.TRAN_PROFIT_CUST_CODE = INDCLIENTS.INDCLIENT_CODE
WHERE S.TRAN_ENTITY_NUM = 1
  AND Y.CMNAEC_EVENT_ENABLED = '1'
  AND X.IACLINK_ACTUAL_ACNUM = '{account_number}'
  AND S.TRAN_DATE_OF_TRAN BETWEEN TO_DATE('{start_date}','DD-MM-YYYY')
                              AND TO_DATE('{end_date}','DD-MM-YYYY')
ORDER BY S.TRAN_DATE_OF_TRAN DESC"""
IACLINK_SQL = "SELECT IACLINK_INTERNAL_ACNUM\nFROM NMBPRODIDC.IACLINK\nWHERE IACLINK_ACTUAL_ACNUM = '{account_number}'"
ACNTS_SMS_SQL = "SELECT ACNTS_SMS_OPERN\nFROM NMBPRODIDC.ACNTS\nWHERE ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"
ACNTS_CURRENCY_SQL = "SELECT ACNTS_CURR_CODE\nFROM NMBPRODIDC.ACNTS\nWHERE ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"
SALARIES_PHONE_SQL = "SELECT account_number, cellphone\nFROM public.customer_accounts_idc\nWHERE account_number = '{account_number}'"
ACNTS_ALERT_PHONE_SQL = """SELECT C.INDCLIENT_TEL_RES
FROM NMBPRODIDC.ACNTS A
JOIN INDCLIENTS C ON C.INDCLIENT_CODE = A.ACNTS_CLIENT_NUM
WHERE A.ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"""
INDCLIENT_EMAIL_SQL = """SELECT C.INDCLIENT_EMAIL_ADDR1
FROM NMBPRODIDC.ACNTS A
JOIN INDCLIENTS C ON C.INDCLIENT_CODE = A.ACNTS_CLIENT_NUM
WHERE A.ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"""
COBICADDR_SQL = """SELECT A.COBICADDR_MOB_NO, A.COBICADDR_SMS_ALERT
FROM NMBPRODIDC.ACNTS AC
JOIN COBICINIT I ON I.COBICINIT_CIF_NO = TO_CHAR(AC.ACNTS_CLIENT_NUM)
JOIN COBICADDR A ON A.COBICADDR_CREF_NO = I.COBICINIT_CREF_NO
WHERE AC.ACNTS_INTERNAL_ACNUM = '{internal_acnum}'"""

TRAN_PATTERN_RE = sql.TRAN_PATTERN_RE

# Rows of standby triggers echoed back to the model (the socket check uses all of them).
MAX_TRIGGERS_SHOWN = 8


@dataclass
class Databases:
    standby: int
    customers: Optional[int]
    socket: Optional[int]
    dispatch: Optional[int] = None


@dataclass
class AlertRegistration:
    mobile_number: Optional[str]
    sms_alert_enabled: Optional[bool]

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "AlertRegistration":
        flag_any: Optional[bool] = None
        for row in rows:
            lowered = {str(k).upper(): v for k, v in row.items()}
            mobile = trim_to_none(lowered.get("COBICADDR_MOB_NO"))
            flag = parse_alert_flag(lowered.get("COBICADDR_SMS_ALERT"))
            if flag is not None and flag_any is None:
                flag_any = flag
            if mobile is not None:
                return cls(mobile, flag if flag is not None else flag_any)
        return cls(None, flag_any)

    @property
    def explicitly_disabled(self) -> bool:
        return self.sms_alert_enabled is False


def _first(rows: list[dict[str, Any]], column: str) -> Optional[str]:
    if not rows:
        return None
    row = rows[0]
    if column in row:
        return trim_to_none(row[column])
    for key, value in row.items():
        if str(key).upper() == column.upper():
            return trim_to_none(value)
    return None


class SmsChecks:
    def __init__(
        self,
        metabase: MetabaseClient,
        dbs: Databases,
        socket_checker: SocketDeliveryChecker,
        source_name: str = "Intellect",
        lookback_days: int = 15,
        salaries_require_delivery_proof: bool = False,
        tran_table_pattern: str = "NMBPRODIDC.TRAN{year}",
    ) -> None:
        if not TRAN_PATTERN_RE.fullmatch(tran_table_pattern):
            raise ValueError("TRAN_TABLE_PATTERN must look like SCHEMA.TRAN{year}")
        self.mb = metabase
        self.dbs = dbs
        self.socket = socket_checker
        self.src = source_name
        self.lookback_days = lookback_days
        self.salaries_require_proof = salaries_require_delivery_proof
        self.tran_table_pattern = tran_table_pattern

    # ---- lookups ----------------------------------------------------------------
    def _q(self, template: str, **values: str) -> list[dict[str, Any]]:
        query = template
        for key, value in values.items():
            query = query.replace("{" + key + "}", value)
        return self.mb.query(self.dbs.standby, query)

    def internal_account_number(self, account: str) -> Optional[str]:
        return _first(self._q(IACLINK_SQL, account_number=sql.oracle_text(sql.digits(account))), "IACLINK_INTERNAL_ACNUM")

    def sms_activation(self, internal: str) -> Optional[str]:
        return _first(self._q(ACNTS_SMS_SQL, internal_acnum=sql.oracle_text(internal)), "ACNTS_SMS_OPERN")

    def account_currency(self, internal: str) -> Optional[str]:
        return _first(self._q(ACNTS_CURRENCY_SQL, internal_acnum=sql.oracle_text(internal)), "ACNTS_CURR_CODE")

    def alert_registration(self, internal: str) -> AlertRegistration:
        try:
            return AlertRegistration.from_rows(self._q(COBICADDR_SQL, internal_acnum=sql.oracle_text(internal)))
        except MetabaseError as exc:
            log.warning("COBICADDR lookup failed for %s; falling back to residential number: %s", internal, exc)
            return AlertRegistration(None, None)

    def account_alert_phone(self, internal: str, registration: AlertRegistration) -> Optional[str]:
        if registration.mobile_number:
            return registration.mobile_number
        return _first(self._q(ACNTS_ALERT_PHONE_SQL, internal_acnum=sql.oracle_text(internal)), "INDCLIENT_TEL_RES")

    def tran_table(self, year: int) -> str:
        return self.tran_table_pattern.replace("{year}", str(int(year)))

    def standby_rows(self, account: str, start: date, end: date) -> list[dict[str, Any]]:
        """Alert triggers in [start, end]. Core banking keeps one TRAN table per year (TRAN2026,
        TRAN2027, ...), so the window is split by year and each slice reads its own table, newest
        first (each slice is already ordered newest first)."""
        account_literal = sql.oracle_text(sql.digits(account))
        rows: list[dict[str, Any]] = []
        succeeded = 0
        last_error: Optional[MetabaseError] = None
        for year in range(end.year, start.year - 1, -1):
            slice_start = max(start, date(year, 1, 1))
            slice_end = min(end, date(year, 12, 31))
            try:
                rows += self._q(
                    STANDBY_SQL,
                    tran_table=self.tran_table(year),
                    account_number=account_literal,
                    start_date=sql.oracle_date(slice_start),
                    end_date=sql.oracle_date(slice_end),
                )
                succeeded += 1
            except MetabaseError as exc:
                # A year's table may not exist yet (first days of January, before rollover).
                # Tolerate that only for a missing table, and only if another year's slice worked.
                if "ORA-00942" not in str(exc):
                    raise
                log.warning("%s not available (%s)", self.tran_table(year), exc)
                last_error = exc
        if succeeded == 0 and last_error is not None:
            raise last_error
        return rows

    def salaries_account(self, account: str) -> Optional[dict[str, Any]]:
        if self.dbs.customers is None:
            raise MetabaseError("CUSTOMERS database is not available")
        query = SALARIES_PHONE_SQL.replace("{account_number}", sql.oracle_text(sql.digits(account)))
        rows = self.mb.query(self.dbs.customers, query)
        return rows[0] if rows else None

    def customer_email(self, internal: str) -> tuple[bool, Optional[str]]:
        """(lookup_ok, email). lookup_ok False when the query failed or returned no row."""
        try:
            rows = self._q(INDCLIENT_EMAIL_SQL, internal_acnum=sql.oracle_text(internal))
        except MetabaseError as exc:
            log.warning("INDCLIENT email lookup failed for %s; keeping escalation: %s", internal, exc)
            return False, None
        if not rows:
            return False, None
        return True, _first(rows, "INDCLIENT_EMAIL_ADDR1")

    # ---- verdicts ------------------------------------------------------------------
    def check_account(
        self, account: str, alert_types: list[str], ticket_phone: Optional[str], as_of: Optional[date] = None
    ) -> dict[str, Any]:
        """SmsAgentService.evaluateTicketAccount."""
        end = as_of or today_local()
        start = end - timedelta(days=self.lookback_days)
        wants_salaries = ALERT_SALARIES in alert_types
        wants_debits = ALERT_DEBITS in alert_types or not wants_salaries
        if wants_salaries and not wants_debits:
            result = self.salaries(account, ticket_phone)
        elif wants_salaries:
            result = self._merge(account, self.salaries(account, ticket_phone),
                                 self.transactional(account, ticket_phone, start, end))
        else:
            result = self.transactional(account, ticket_phone, start, end)
        result["window"] = {"start_date": start.strftime("%d-%m-%Y"), "end_date": end.strftime("%d-%m-%Y")}
        result["alert_types_checked"] = [t for t in (ALERT_SALARIES, ALERT_DEBITS)
                                         if (t == ALERT_SALARIES and wants_salaries) or (t == ALERT_DEBITS and wants_debits)]
        result["auto_close_allowed"] = result["status"] not in ESCALATING_STATUSES
        return result

    def salaries(self, account: str, ticket_phone: Optional[str]) -> dict[str, Any]:
        internal = self.internal_account_number(account)
        currency = self.account_currency(internal) if internal else None
        row = self.salaries_account(account)
        db_phone = trim_to_none(row.get("cellphone")) if row else None
        status, reply = self._salaries_reply(account, ticket_phone, row is not None, db_phone)
        return {
            "account_number": account,
            "alert_type": ALERT_SALARIES,
            "status": status,
            "reply": reply,
            "internal_account_number": internal,
            "account_currency": currency,
            "customer_account_found": row is not None,
            "db_phone": db_phone,
            "normalized_db_phone": normalize_ticket_phone(db_phone),
        }

    def _salaries_reply(self, account: str, ticket_phone: Optional[str], found: bool, db_phone: Optional[str]) -> tuple[str, str]:
        if ticket_phone is not None and not is_valid_ticket_phone(ticket_phone):
            return "invalid_ticket_phone", f"Invalid phone number on the ticket: {ticket_phone}."
        if not found:
            return ("salaries_account_missing",
                    f"The account {account} is not maintained on customer_accounts_idc. "
                    "Kindly advise ICT Data science to update the Database.")
        normalized_ticket = normalize_ticket_phone(ticket_phone)
        normalized_db = normalize_ticket_phone(db_phone)
        display = trim_to_none(db_phone) or "NULL"
        if not is_deliverable_phone(normalized_db):
            return ("salaries_phone_missing",
                    f"There is no valid mobile number on file for this account on customer_accounts_idc ({display}). "
                    "Kindly advise ICT Data science to update the mobile number on the Database.")
        if normalized_db != normalized_ticket:
            return ("salaries_phone_mismatch",
                    f"The phone number on the ticket does not match the one on customer_accounts_idc ({display}). "
                    "Kindly advise ICT Data science to update the mobile number on the Database.")
        if self.salaries_require_proof:
            return ("salaries_unverified",
                    f"The phone number on the ticket matches customer_accounts_idc for account {account}, but salary "
                    "alerts have no trigger record that can be reconciled against the SMS gateway, so delivery is "
                    "unconfirmed. Do not advise the client to check their blocked SMS folder until a send is confirmed. "
                    "Check the SMS queue monitor for traffic to this number.")
        return "salaries_match", "Kindly advise the client to check blocked SMS folder."

    def transactional(self, account: str, ticket_phone: Optional[str], start: date, end: date) -> dict[str, Any]:
        base: dict[str, Any] = {"account_number": account, "alert_type": ALERT_DEBITS}
        internal = self.internal_account_number(account)
        if internal is None:
            return {**base, "status": "invalid_account_number", "reply": f"Invalid account number: {account}.",
                    "internal_account_number": None}
        base["internal_account_number"] = internal
        base["account_currency"] = self.account_currency(internal)
        oper = self.sms_activation(internal)
        activated = is_sms_activated(oper)
        base["sms_oper_value"] = oper
        base["sms_activated"] = activated
        if activated is None:
            return {**base, "status": "sms_activation_unknown",
                    "reply": f"SMS account activation could not be confirmed for the account: {account}."}
        if not activated:
            return {**base, "status": "sms_not_enabled",
                    "reply": f"Kindly enable the SMS alerts at account level for the account: {account}."}

        registration = self.alert_registration(internal)
        base["cob_sms_alert"] = registration.sms_alert_enabled
        base["cob_mobile_number"] = registration.mobile_number
        if registration.explicitly_disabled:
            return {**base, "status": "sms_registration_disabled", "reply": (
                "The mobile number on file is correct, but SMS-alert registration is switched OFF for this customer "
                "on IDCCOB (COBICADDR_SMS_ALERT = false). Kindly enable the customer's SMS-alert registration on "
                "IDCCOB and confirm. The number and account are correct, so no SMS gateway or handset action is required.")}

        rows = self.standby_rows(account, start, end)
        base["standby_trigger_count"] = len(rows)
        base["standby_numbers"] = sorted({str(r.get("INDCLIENT_TEL_RES")).strip() for r in rows
                                          if trim_to_none(r.get("INDCLIENT_TEL_RES"))})
        base["recent_triggers"] = [
            {"at": format_timestamp(parse_timestamp(r.get("UPTD_ON"))), "event": r.get("CMNAEC_EVENT_NAME"),
             "currency": r.get("TRAN_CURR_CODE"), "amount": r.get("TRAN_AMOUNT")}
            for r in rows[:MAX_TRIGGERS_SHOWN]
        ]
        if not rows:
            account_phone = self.account_alert_phone(internal, registration)
            base["account_phone"] = account_phone
            status, reply = self._reply_no_transactions(ticket_phone, account_phone)
        else:
            status, reply = self._reply_for_account(ticket_phone, rows)

        base["socket_delivery"] = None
        if status == "match":
            report = self.socket.check(self.dbs.socket, [AlertTrigger.from_standby_row(r) for r in rows])
            base["socket_delivery"] = report.to_dict()
            status, reply = self._socket_verdict(account, report.status, report.detail)

        if status == "socket_not_found":
            ok, email = self.customer_email(internal)
            if ok:
                base["customer_email_usable"] = is_usable_customer_email(email)
                if not is_usable_customer_email(email):
                    status, reply = "sms_no_customer_email", (
                        f"The number and account are correct, and the alert(s) for account {account} were generated "
                        "in core banking. However, the customer has no valid email address on file in the core "
                        "customer profile, which the alert platform currently requires to dispatch an SMS (a known "
                        "issue with the platform, permanent fix in progress). INTERIM ACTION: kindly capture a valid "
                        "email address for the customer at the branch (customer master / INDCLIENT_EMAIL_ADDR1) and "
                        "SMS alerts will resume. A placeholder such as none@nmbz.co.zw will not work, and no handset "
                        "or SMS-gateway action is required.")
        return {**base, "status": status, "reply": reply}

    def _reply_for_account(self, ticket_phone: Optional[str], rows: list[dict[str, Any]]) -> tuple[str, str]:
        if ticket_phone is not None and not is_valid_ticket_phone(ticket_phone):
            return "invalid_ticket_phone", f"Invalid phone number on the ticket: {ticket_phone}."
        raw_numbers: list[str] = []
        for row in rows:
            value = row.get("INDCLIENT_TEL_RES")
            if value is not None and str(value) not in raw_numbers:
                raw_numbers.append(str(value))
        normalized = {n for n in (normalize_ticket_phone(v) for v in raw_numbers) if n}
        if not normalized:
            return "no_number", (f"There is no residential/mobile number maintained for this CIF on {self.src}. "
                                 f"Kindly capture the client's correct mobile number on {self.src}.")
        ticket_normalized = normalize_ticket_phone(ticket_phone)
        if ticket_normalized is not None and ticket_normalized not in normalized:
            numbers = ", ".join(v for v in raw_numbers if v.strip())
            return "different_number", (f"The number on the ticket differs from the number maintained on {self.src} "
                                         f"({numbers}). Kindly update the client's correct mobile number on {self.src}.")
        if ticket_normalized is not None:
            return "match", (f"The number on the ticket matches the number maintained on {self.src}. "
                             "Please advise the client to check the SMS spam folder or blocked SMS folder.")
        return "results_found", (f"SMS-enabled transactions were found for this account on {self.src} "
                                 f"in the previous {self.lookback_days} days.")

    def _reply_no_transactions(self, ticket_phone: Optional[str], account_phone: Optional[str]) -> tuple[str, str]:
        no_txn = (f"No SMS-enabled transactions were found for this account on {self.src} "
                  f"in the previous {self.lookback_days} days. ")
        if ticket_phone is not None and not is_valid_ticket_phone(ticket_phone):
            return "invalid_ticket_phone", f"Invalid phone number on the ticket: {ticket_phone}."
        normalized_account = normalize_ticket_phone(account_phone)
        if normalized_account is None:
            return "no_txn_no_number", (
                no_txn + f"There is also no mobile number maintained for this client on {self.src}, so no alert can be "
                f"delivered even when a transaction occurs. Kindly capture the client's correct mobile number on {self.src}.")
        normalized_ticket = normalize_ticket_phone(ticket_phone)
        if normalized_ticket is not None and normalized_ticket != normalized_account:
            return "no_txn_different_number", (
                no_txn + f"The number on the ticket also differs from the number maintained on {self.src} "
                f"({trim_to_none(account_phone)}). Kindly update the client's correct mobile number on {self.src}.")
        return "no_txn_number_match", (
            no_txn + f"The number on the ticket matches the number maintained on {self.src}, so the account is correctly "
            "set up to receive alerts; there was simply no qualifying activity in the window.")

    def _socket_verdict(self, account: str, status: str, detail: str) -> tuple[str, str]:
        if status == DELIVERED:
            return "delivered_check_spam", (
                f"The number on the ticket matches the number maintained on {self.src}, and the SMS gateway confirmed "
                f"the alert(s) for account {account} were delivered. Kindly advise the client to check the SMS spam "
                "folder or blocked SMS folder, and to confirm that our sender ID is not blocked on the handset.")
        if status == PENDING:
            return "socket_pending", (
                f"The number on the ticket is correct, but the alert(s) for account {account} are still queued at the "
                "SMS gateway and have not been delivered. This is a delivery-queue delay on our side, not a handset "
                f"issue. {detail}")
        if status == FAILED:
            return "socket_failed", (
                f"The number on the ticket is correct, but the SMS gateway failed to send the alert(s) for account "
                f"{account}. The client will not find these messages in any folder. {detail}")
        if status == NOT_FOUND:
            return "socket_not_found", (
                f"The number on the ticket is correct and the alert(s) for account {account} were generated in core "
                "banking, but there is no matching record at the SMS gateway, so the message was never sent. "
                f"Escalate to the SMS/CAE queue owners. {detail}")
        return "socket_unknown", (
            f"The number on the ticket matches the number maintained on {self.src}, but gateway delivery for account "
            f"{account} could not be confirmed. {detail}")

    def _merge(self, account: str, salaries: dict[str, Any], debits: dict[str, Any]) -> dict[str, Any]:
        if salaries["status"] in ESCALATING_STATUSES:
            status = salaries["status"]
        elif debits["status"] in ESCALATING_STATUSES:
            status = debits["status"]
        else:
            status = "both_alert_types_checked"
        return {
            "account_number": account,
            "alert_type": "BOTH",
            "status": status,
            "reply": f"Salaries — {salaries['reply']} Debits — {debits['reply']}",
            "internal_account_number": debits.get("internal_account_number") or salaries.get("internal_account_number"),
            "account_currency": debits.get("account_currency") or salaries.get("account_currency"),
            "salaries_result": salaries,
            "debits_result": debits,
            "socket_delivery": debits.get("socket_delivery"),
        }

    # ---- pipeline trace (AlertDeliveryInvestigator) ----------------------------------
    def trace_pipeline(self, mobile: str, transaction_date: Optional[date] = None) -> dict[str, Any]:
        """CAE request -> CAE response -> dispatch -> gateway, for one mobile. Diagnostic only."""
        sub = subscriber(normalize_gateway_phone(mobile) or mobile)
        if sub is None:
            raise ValueError("not a Zimbabwean mobile number")
        sub = sql.digits(sub)
        today = today_local()
        if transaction_date is not None:
            days = [d for d in (transaction_date + timedelta(days=o) for o in (-1, 0, 1)) if d <= today] or [today]
        else:
            days = [today - timedelta(days=b) for b in range(3)]
        days.sort(reverse=True)
        start, end_exclusive = days[-1], days[0] + timedelta(days=1)

        def safe(db: Optional[int], query: str) -> Optional[list[dict[str, Any]]]:
            if db is None:
                return None
            try:
                return self.mb.query(db, query)
            except MetabaseError as exc:
                log.warning("Pipeline trace query failed: %s", exc)
                return None

        req_total = req_success = 0
        refs: list[str] = []
        for day in days:
            for row in safe(self.dbs.standby, (
                "SELECT REFERENCE_NO, REQ_STATUS FROM NMBPRODCAE.AUDIT_LETTER_MGMT_REQ"
                f" WHERE CREATED_ON_DT = DATE '{day.isoformat()}'"
                f" AND DBMS_LOB.SUBSTR(REQUEST_DATA, 2000, 1) LIKE '%{sub}%'")) or []:
                req_total += 1
                if str(row.get("REQ_STATUS") or "").upper() == "SUCCESS":
                    req_success += 1
                    ref = trim_to_none(row.get("REFERENCE_NO"))
                    if ref:
                        refs.append(ref)

        res_rows = 0
        for i in range(0, len(refs), 200):
            conditions = " OR ".join(
                f"(reference_no = '{sql.oracle_text(r)}' OR reference_no LIKE '{sql.oracle_text(sql.like_fragment(r))}-%')"
                for r in refs[i:i + 200]
            )
            res_rows += len(safe(self.dbs.standby, f"SELECT reference_no FROM NMBPRODCAE.AUDIT_LETTER_MGMT_RES WHERE {conditions}") or [])

        dispatch_rows = safe(self.dbs.dispatch, (
            "SELECT m.id, m.dispatch_status FROM sms_message m JOIN sms_recipient r ON r.id = m.recipient_id"
            f" WHERE r.mobile_number LIKE '%{sub}%'"
            f" AND m.created_at >= DATE '{start.isoformat()}' AND m.created_at < DATE '{end_exclusive.isoformat()}'"))
        dispatch_total = len(dispatch_rows or [])
        dispatch_sent = sum(1 for r in dispatch_rows or [] if str(r.get("dispatch_status") or "").upper() == "SENT")

        socket_rows = safe(self.dbs.socket, (
            "SELECT external_status FROM public.transaction_activity"
            " WHERE service_type = 'VALUE_ADDED_SERVICES' AND service_operation_type = 'SEND_SMS'"
            f" AND time_stamp >= TIMESTAMP '{start.isoformat()} 00:00:00'"
            f" AND time_stamp <  TIMESTAMP '{end_exclusive.isoformat()} 00:00:00'"
            f" AND logged_responses LIKE '%{sub}%'"))
        socket_total = None if socket_rows is None else len(socket_rows)
        socket_delivered = None if socket_rows is None else sum(
            1 for r in socket_rows if str(r.get("external_status") or "").upper() == "DELIVERED_SUCCESSFULLY")

        email: Optional[str] = None
        if req_success > 0 and res_rows == 0:
            rows = safe(self.dbs.standby, (
                "SELECT ind.INDCLIENT_EMAIL_ADDR1 addr1 FROM COBICADDR a"
                " JOIN COBICINIT i ON i.COBICINIT_CREF_NO = a.COBICADDR_CREF_NO"
                " JOIN NMBPRODIDC.INDCLIENTS ind ON ind.INDCLIENT_CODE = i.COBICINIT_CIF_NO"
                f" WHERE a.COBICADDR_MOB_NO LIKE '%{sub}%' AND ROWNUM <= 1")) or []
            email = _first(rows, "addr1") if rows else None

        return {
            "subscriber": sub,
            "window": f"{start.isoformat()} .. {days[0].isoformat()}",
            "stage_1_cae_request_total": req_total,
            "stage_1_cae_request_success": req_success,
            "stage_2_cae_response_rows": res_rows,
            "stage_3_dispatch_total": dispatch_total if dispatch_rows is not None else None,
            "stage_3_dispatch_sent": dispatch_sent if dispatch_rows is not None else None,
            "stage_4_socket_total": socket_total,
            "stage_4_socket_delivered": socket_delivered,
            "customer_email_on_file": None if not (req_success > 0 and res_rows == 0) else bool(email and email.strip()),
            "verdict": _pipeline_verdict(req_total, req_success, res_rows, dispatch_total, dispatch_sent,
                                         socket_delivered, email, dispatch_rows is not None),
        }


def _pipeline_verdict(req_total: int, req_success: int, res_rows: int, dispatch_total: int, dispatch_sent: int,
                      socket_delivered: Optional[int], email: Optional[str], dispatch_available: bool) -> str:
    if req_success == 0:
        if req_total == 0:
            return ("NO ALERT TRIGGERED in the window - no CAE request for this number. Either there was no alertable "
                    "transaction, or the trigger never fired. If the customer definitely transacted, escalate to the "
                    "core/EVTSLOG alert-generation owners.")
        return "REQUEST NOT ACCEPTED - CAE requests exist but none are SUCCESS. Escalate to the CAE team."
    if res_rows == 0:
        if not (email and email.strip()):
            return ("BREAK at CAE letter-render - ROOT CAUSE CONFIRMED: the customer's INDCLIENT_EMAIL_ADDR1 is NULL, so "
                    "CAE builds the request with TO=null and the alert is never produced (no dispatch, no socket, no SMS). "
                    "BANK-SIDE CAE fault, not handset/registration. ACTION (interim): populate INDCLIENT_EMAIL_ADDR1 with a "
                    "valid email, then re-verify. ACTION (permanent): CAE to tolerate a null TO. Escalate to CAE / "
                    "alert-processing.")
        return ("BREAK at CAE letter-render: request accepted but no AUDIT_LETTER_MGMT_RES, even though "
                "INDCLIENT_EMAIL_ADDR1 is populated - a CAE render issue other than the null-TO cause. ACTION: escalate "
                "to CAE / letter-processing.")
    if not dispatch_available:
        return "CAE rendered the alert; the dispatch database could not be read, so later stages are unknown."
    if dispatch_total == 0:
        return ("BREAK at CAE->dispatch forward: the letter was rendered but never reached the SMS dispatch service. "
                "ACTION: escalate to the CAE / dispatch owners.")
    if dispatch_sent == 0:
        return ("BREAK at dispatch service: the message is present but stuck/failed (not SENT). ACTION: escalate to the "
                "SMS dispatch-service owners.")
    if socket_delivered is None:
        return ("REACHED THE PROVIDER (SENT to the gateway); delivery could not be confirmed (socket lookup failed or "
                "timed out). ACTION: retry; if still SENT-not-delivered, escalate to the SMS gateway.")
    if socket_delivered == 0:
        return ("BREAK at provider/gateway: SENT to the provider but no successful delivery at the gateway. ACTION: "
                "escalate to the SMS gateway / provider owners.")
    return ("DELIVERED - the alert reached the handset (DELIVERED_SUCCESSFULLY at the gateway). Handset-side issue: "
            "advise the customer to check the blocked/spam SMS folder and unblock the bank's SMS sender.")
