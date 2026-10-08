"""Where a failed transaction can be seen, through Metabase, read-only:

- POST OFFICE (Postilion, SQL Server) post_tran: card and switch traffic (ZIPIT, bank-to-wallet and wallet-to-bank,
  POS, ATM, airtime bought with a card). Response code, reversal flag, RRN and STAN.
- CONECT 2.0 and BILLERS (PostgreSQL): NMB Connect / USSD / app transactions, Zimswitch payments, and the biller
  tables (Econet, NetOne, Telecel airtime; ZESA, City of Harare, offline billers such as ZIMRA).
- IB (MySQL): internet banking RTGS and internal transfers.
- STANDBY DB (Oracle standby of core banking): the postings on the account (TRAN<year>), to see whether the client
  was actually debited and whether the money came back. Never the live core banking system.

Every source returns Txn objects with one reading (succeeded, declined, reversed, ...) worked out here, in code,
from the system's own codes; the model reads these readings, it does not invent them. Ported from the Java
investigators (PostilionTransactionInvestigator, NmbConnectTransactionInvestigator, IbTransactionInvestigator,
FailedTransactionInvestigator), with the fixes the review found (cents, reversal flag "2", the 25-row cut before
the amount filter).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    account_forms,
    account_key,
    currency_name,
    first,
    format_timestamp,
    money,
    parse_timestamp,
    to_amount,
    today_local,
    trim_to_none,
)

log = logging.getLogger(__name__)

# What a transaction's record says happened. Only succeeded, declined and reversed can ever close a ticket, and
# failed only once core banking shows the client was not debited (or was refunded).
SUCCEEDED, DECLINED, FAILED, REVERSED = "succeeded", "declined", "failed", "reversed"
REVERSAL_PENDING, REVERSAL_FAILED, UNCERTAIN, POSTED = "reversal_pending", "reversal_failed", "uncertain", "posted"
READINGS = {
    SUCCEEDED: "went through: the money moved",
    DECLINED: "declined before any money moved (the reason is in the code)",
    FAILED: "failed; whether the client was debited must be checked in core banking",
    REVERSED: "failed and was reversed: the money went back",
    REVERSAL_PENDING: "failed and a reversal is queued but not confirmed",
    REVERSAL_FAILED: "failed and the automatic reversal also failed: the client may be out of pocket",
    UNCERTAIN: "no final answer (timeout, pending or incomplete): the client may have been debited",
    POSTED: "a posting on the account in core banking (debit or credit), with no status of its own",
}

# ---- Postilion -------------------------------------------------------------------------------------------
APPROVED_CODES = {"00", "000", "0"}
UNCERTAIN_CODES = {"68", "91", "96", "98"}  # no definitive answer: possible debit downstream
RSP_MEANINGS = {
    "00": "approved", "01": "refer to card issuer", "03": "invalid merchant", "05": "do not honour",
    "12": "invalid transaction", "13": "invalid amount", "14": "invalid account number", "30": "format error",
    "41": "lost card", "43": "stolen card", "51": "insufficient funds", "54": "expired card", "55": "incorrect PIN",
    "57": "transaction not permitted to cardholder", "58": "transaction not permitted on terminal",
    "61": "exceeds the withdrawal/transaction amount limit", "62": "restricted card",
    "65": "exceeds the withdrawal frequency limit", "68": "response received too late", "75": "PIN tries exceeded",
    "91": "issuer or switch inoperative (timeout)", "94": "duplicate transaction", "96": "system malfunction",
    "98": "unable to process",
}
TRAN_TYPES = {"00": "purchase", "01": "cash withdrawal", "09": "purchase with cashback", "20": "refund",
              "21": "deposit", "40": "transfer", "50": "payment"}
INQUIRY_TYPES = ("30", "31")  # balance and available-funds inquiries: never a disputed transaction
POSTILION_COLUMNS = ("post_tran_id, message_type, tran_type, tran_completed, rsp_code_rsp, abort_rsp_code, "
                     "tran_reversed, prev_post_tran_id, tran_amount_req, tran_currency_code, datetime_tran_local, "
                     "retrieval_reference_nr, system_trace_audit_nr, from_account_id, to_account_id")


def rsp_meaning(code: Optional[str]) -> str:
    code = (code or "").strip()
    return RSP_MEANINGS.get(code, f"response code {code}" if code else "no response code")


@dataclass
class Txn:
    """One transaction as one system recorded it."""
    id: str                       # T1, T2, ... (this ticket's lookups; never a database key)
    source: str                   # postilion | conect | billers | ib | core
    channel: str                  # what kind: "ZIPIT / switch purchase", "Econet airtime", "core posting", ...
    when: Optional[datetime]
    amount: Optional[Decimal]     # major units (dollars), whatever the system stores
    currency: Optional[str]
    account: Optional[str]        # the client's account as the system holds it
    status: str                   # one of READINGS
    detail: str                   # the system's own code and what it means
    counterparty: Optional[str] = None
    reference: Optional[str] = None
    trace: Optional[str] = None
    debit: Optional[bool] = None  # core postings only
    reversal: Optional[str] = None  # what shows the money went back (flag, reversal row, refund posting)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def reading(self) -> str:
        return READINGS.get(self.status, self.status)

    def describe(self) -> dict[str, Any]:
        out = {"id": self.id, "source": self.source, "channel": self.channel, "when": format_timestamp(self.when),
               "amount": money(self.amount, self.currency), "account": self.account, "status": self.status,
               "reading": self.reading, "detail": self.detail}
        for key in ("counterparty", "reference", "trace", "reversal"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.debit is not None:
            out["direction"] = "debit" if self.debit else "credit"
        return out

    def same_amount(self, amount: Optional[Decimal]) -> bool:
        return amount is not None and self.amount is not None and abs(self.amount - amount) <= Decimal("0.01")


@dataclass
class Window:
    start: date
    end: date  # inclusive

    @classmethod
    def around(cls, day: Optional[date], days: int, lookback: int, today: Optional[date] = None) -> "Window":
        today = today or today_local()
        if day is None:
            return cls(today - timedelta(days=lookback), today)
        return cls(day - timedelta(days=days), min(day + timedelta(days=days), today))

    def describe(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


def _accounts_in(account: str, quote: Any = sql.mysql_text) -> str:
    forms = account_forms(account)
    if not forms:
        raise sql.UnsafeValue("not an account number")
    return ", ".join(f"'{quote(sql.digits(f))}'" for f in forms)


class Postilion:
    """POST OFFICE post_tran. Amounts are stored in cents; each switch transaction can appear once per leg."""

    def __init__(self, mb: MetabaseClient, db: int, limit: int = 80) -> None:
        self.mb, self.db, self.limit = mb, db, limit

    def by_account(self, account: str, window: Window, amount: Optional[Decimal] = None) -> list[Txn]:
        accounts = _accounts_in(account)
        where = [f"(from_account_id IN ({accounts}) OR to_account_id IN ({accounts}))",
                 f"datetime_tran_local >= '{sql.iso_date(window.start)}'",
                 f"datetime_tran_local < '{sql.iso_date(window.end + timedelta(days=1))}'",
                 f"tran_type NOT IN ('{INQUIRY_TYPES[0]}', '{INQUIRY_TYPES[1]}')"]
        if amount is not None:  # filter in SQL (cents), so a busy account cannot push the right row off the list
            cents = int((amount * 100).to_integral_value())
            where.append(f"tran_amount_req BETWEEN {cents - 1} AND {cents + 1}")
        return self._read(" AND ".join(where))

    def by_reference(self, reference: str) -> list[Txn]:
        """By RRN (the STAN repeats across transactions, so it is not used on its own)."""
        return self._read(f"retrieval_reference_nr = '{sql.mysql_text(reference)}'")

    def _read(self, where: str) -> list[Txn]:
        rows = self.mb.query(self.db, f"SELECT TOP {int(self.limit)} {POSTILION_COLUMNS}\nFROM post_tran WITH (NOLOCK)\n"
                                      f"WHERE {where}\nORDER BY datetime_tran_local DESC")
        return postilion_transactions(rows)


def _flag(value: Any) -> int:
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "t", "y", "yes"):
        return 1
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def postilion_transactions(rows: list[dict[str, Any]]) -> list[Txn]:
    """One Txn per switch transaction: legs are merged, reversal advices (04xx) are folded into the original."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        key = (trim_to_none(row.get("retrieval_reference_nr")) or str(row.get("post_tran_id")),
               trim_to_none(row.get("system_trace_audit_nr")),
               to_amount(row.get("tran_amount_req")))
        groups.setdefault(key, []).append(row)
    out: list[Txn] = []
    for members in groups.values():
        originals = [r for r in members if not str(r.get("message_type") or "").startswith("04")] or members
        advices = [r for r in members if str(r.get("message_type") or "").startswith("04")]
        row = originals[0]
        rsp = trim_to_none(row.get("rsp_code_rsp")) or ""
        reversed_flag = max(_flag(r.get("tran_reversed")) for r in members)  # 0 no, 1 partial, 2 full
        completed = all(_flag(r.get("tran_completed")) for r in originals)
        abort = trim_to_none(row.get("abort_rsp_code"))
        cents = to_amount(row.get("tran_amount_req"))
        detail = f"response {rsp or '-'} ({rsp_meaning(rsp)})"
        if abort and abort != "0":
            detail += f"; Postilion aborted it (abort code {abort})"
        reversal = None
        if reversed_flag == 2 or (advices and reversed_flag != 1):
            status = REVERSED
            reversal = "Postilion marks it fully reversed" if reversed_flag == 2 else "a reversal advice (04xx) follows it"
        elif reversed_flag == 1:
            status, detail = UNCERTAIN, detail + "; only partly reversed"
        elif rsp in APPROVED_CODES and completed:
            status = SUCCEEDED
        elif rsp in UNCERTAIN_CODES or not completed:
            status = UNCERTAIN
        else:
            status = DECLINED
        tran_type = str(row.get("tran_type") or "").strip()
        accounts = [trim_to_none(row.get("from_account_id")), trim_to_none(row.get("to_account_id"))]
        out.append(Txn(
            id="", source="postilion", channel=f"card/switch {TRAN_TYPES.get(tran_type, f'type {tran_type}')}",
            when=parse_timestamp(row.get("datetime_tran_local")),
            amount=(cents / 100).quantize(Decimal("0.01")) if cents is not None else None,
            currency=currency_name(row.get("tran_currency_code")), account=accounts[0] or accounts[1], status=status,
            detail=detail, counterparty=accounts[1] if accounts[0] else None,
            reference=trim_to_none(row.get("retrieval_reference_nr")), trace=trim_to_none(row.get("system_trace_audit_nr")),
            reversal=reversal, raw={"legs": len(members), "reversal_advices": len(advices)},
        ))
    out.sort(key=lambda t: t.when or datetime.min, reverse=True)
    return out


# ---- NMB Connect (CONECT 2.0) and the billers ---------------------------------------------------------------
@dataclass(frozen=True)
class Table:
    db: str            # "conect" or "billers"
    name: str
    accounts: tuple    # columns holding the client's account
    date_col: Optional[str]
    label: str
    type_filter: Optional[str] = None  # transaction_type for CONECT transactions


CONECT_TXNS = ("conect", "transactions", ("account_number", "beneficiary_account"), "created_date")
TABLES = {
    "rtgs": [Table(*CONECT_TXNS, "NMB Connect RTGS", "RTGS")],
    "transfer": [Table(*CONECT_TXNS, "NMB Connect internal transfer", "IFT")],
    "zipit": [Table(*CONECT_TXNS, "NMB Connect ZIPIT", "ZIPIT"),
              Table("conect", "zimswitch_payment", ("source_account",), "created_at", "Zimswitch payment")],
    "airtime": [Table(*CONECT_TXNS, "NMB Connect airtime/bundle"),  # operation AIRTIME_PURCHASE, *_BUNDLE_PAY
                Table("billers", "econet_payment", ("source_account",), "transaction_date", "Econet airtime"),
                Table("billers", "netone_payment", ("source_account",), "transaction_date", "NetOne airtime"),
                Table("billers", "telecel_payment", ("source_account",), "transaction_date", "Telecel airtime")],
    "bill": [Table(*CONECT_TXNS, "NMB Connect bill payment", "BILLER_PAYMENT"),
             Table("billers", "esol_zesa_payment", ("source_account",), "date_created", "ZESA token"),
             Table("billers", "city_of_harare_transactions", ("source_account",), "date_created", "City of Harare"),
             Table("billers", "offline_bill_payment", ("source_account_number",), "date_created", "Offline biller")],
    "zimra": [Table(*CONECT_TXNS, "NMB Connect bill payment", "BILLER_PAYMENT"),
              Table("billers", "offline_bill_payment", ("source_account_number",), "date_created", "Offline biller")],
}
KINDS = tuple(TABLES)
AMOUNT_KEYS = ("amount", "transaction_amount", "checkout_amount")
STATUS_KEYS = ("status", "main_trans_status", "t24status", "zss_status", "reversal_status", "principal_transfer_status")
CODE_KEYS = ("error_code", "response_code", "result_code", "status_code", "vendor_response_code")
MESSAGE_KEYS = ("response_message", "result_description", "response_description", "reason",
                "vendor_response_message", "portal_description", "message")
REF_KEYS = ("core_banking_reference", "transaction_id", "zipit_reference", "payment_reference", "shared_reference",
            "receipt_number", "transaction_reference", "vendor_reference", "trans_no", "bank_ref_number", "payment_id",
            "reference_number", "reference")
TYPE_KEYS = ("operation", "transaction_type", "biller_name", "product_name", "provider", "destination_institution")
RECIPIENT_KEYS = ("beneficiary_account", "destination_account", "beneficiary_mobile", "destination_phone",
                  "mobile_number", "meter_number", "bill_account", "utility_account", "customer_number")
CURRENCY_KEYS = ("currency", "debit_currency", "source_currency", "payment_currency", "currency_code")
NO_FUNDS_CODES = {"51"}  # insufficient funds: declined before any debit


def connect_reading(row: dict[str, Any]) -> tuple[str, str]:
    """(status, detail) from the status, code and message columns CONECT and the billers use."""
    status = (first(row, STATUS_KEYS) or "").upper()
    code = first(row, CODE_KEYS)
    message = first(row, MESSAGE_KEYS)
    detail = "status " + (status or "(none)") + (f", code {code}" if code else "") + (f": {message[:160]}" if message else "")
    if status in ("SUCCESS", "SUCCESSFUL", "COMPLETED", "APPROVED"):
        return SUCCEEDED, detail
    if status == "REVERSED" or (code or "").upper() == "REVERSAL_SUCCESSFUL":
        return REVERSED, detail
    if status == "REVERSAL_FAILED":
        return REVERSAL_FAILED, detail
    if status.startswith("FLAG") or status in ("FLAGED_FOR_RECON", "FLAGGED_FOR_RECON"):
        return REVERSAL_PENDING, detail
    if status in ("PENDING", "PROCESSING", "INITIATED", "INCOMPLETE") or "timed out" in (code or message or "").lower():
        return UNCERTAIN, detail
    if status in ("FAILED", "DECLINED", "REJECTED", "ERROR"):
        if (code or "").strip() in NO_FUNDS_CODES:
            return DECLINED, detail + " (insufficient funds)"
        return FAILED, detail
    return UNCERTAIN, detail


class Connect:
    def __init__(self, mb: MetabaseClient, conect_db: Optional[int], billers_db: Optional[int], limit: int = 25) -> None:
        self.mb, self.limit = mb, limit
        self.dbs = {"conect": conect_db, "billers": billers_db}

    def find(self, account: str, kind: str, window: Window, amount: Optional[Decimal] = None) -> tuple[list[Txn], list[str]]:
        """(transactions, tables that could not be read)."""
        tables = TABLES.get(kind) or [t for k in KINDS for t in TABLES[k]]
        seen, out, failed = set(), [], []
        for table in tables:
            key = (table.db, table.name, table.type_filter)
            if key in seen:
                continue
            seen.add(key)
            db = self.dbs.get(table.db)
            if db is None:
                failed.append(f"{table.db}.{table.name} (database not available)")
                continue
            try:
                rows = self.mb.query(db, self._sql(table, account, window))
            except MetabaseError as exc:  # a schema difference must show, not read as "nothing found"
                failed.append(f"{table.db}.{table.name}: {str(exc)[:120]}")
                continue
            for row in rows:
                txn = self._txn(table, row)
                if kind == "airtime" and table.name == "transactions" and not _is_airtime(row):
                    continue
                if amount is None or txn.same_amount(amount) or txn.amount is None:
                    out.append(txn)
        out.sort(key=lambda t: t.when or datetime.min, reverse=True)
        return out, failed

    def _sql(self, table: Table, account: str, window: Window) -> str:
        accounts = _accounts_in(account)
        where = "(" + " OR ".join(f"{col} IN ({accounts})" for col in table.accounts) + ")"
        if table.type_filter:
            where += f" AND transaction_type = '{sql.mysql_text(table.type_filter)}'"
        if table.date_col:
            where += (f" AND {table.date_col} >= '{sql.iso_date(window.start)}'"
                      f" AND {table.date_col} < '{sql.iso_date(window.end + timedelta(days=1))}'")
        order = f" ORDER BY {table.date_col} DESC" if table.date_col else ""
        return f"SELECT * FROM {table.name} WHERE {where}{order} LIMIT {int(self.limit)}"

    def _txn(self, table: Table, row: dict[str, Any]) -> Txn:
        status, detail = connect_reading(row)
        kind = first(row, TYPE_KEYS)
        when = parse_timestamp(row.get(table.date_col)) if table.date_col else None
        return Txn(
            id="", source=table.db, channel=table.label + (f" ({kind})" if kind and kind.upper() not in table.label.upper() else ""),
            when=when, amount=to_amount(first(row, AMOUNT_KEYS)), currency=currency_name(first(row, CURRENCY_KEYS)),
            account=first(row, table.accounts), status=status, detail=detail,
            counterparty=first(row, RECIPIENT_KEYS), reference=first(row, REF_KEYS),
            reversal=detail if status == REVERSED else None, raw={"table": f"{table.db}.{table.name}"},
        )


def _is_airtime(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("operation", "transaction_type", "biller_name")).upper()
    return any(word in text for word in ("AIRTIME", "BUNDLE", "TOPUP", "TOP_UP"))


# ---- Internet banking -------------------------------------------------------------------------------------
IB_TABLES = {"rtgstransaction": "Internet banking RTGS", "internalfundstransfer": "Internet banking transfer",
             "own_internalfundstransfer": "Internet banking own-account transfer"}
IB_COLUMNS = ("transaction_status, is_txn_reversed, transaction_value, transaction_reference, "
              "transaction_system_reference, account_identification1, account_identification2, "
              "beneficiary_account_number, date_created")


def ib_reading(status: Any, reversed_flag: Any) -> tuple[str, str]:
    text = str(status or "").strip()
    if _flag(reversed_flag):
        return REVERSED, f"status {text or '(none)'}, marked reversed"
    low = text.lower()
    if low in ("successful", "success"):
        return SUCCEEDED, f"status {text}"
    if low in ("failed", "declined"):
        # IB reversals live in FtReversals, not on this row; whether money moved is checked in core banking
        return FAILED, f"status {text}"
    return UNCERTAIN, f"status {text or '(none)'}: did not complete"


class InternetBanking:
    def __init__(self, mb: MetabaseClient, db: int, limit: int = 25) -> None:
        self.mb, self.db, self.limit = mb, db, limit

    def find(self, account: str, window: Window, amount: Optional[Decimal] = None) -> list[Txn]:
        accounts = _accounts_in(account)
        out: list[Txn] = []
        for table, label in IB_TABLES.items():
            where = (f"(account_identification1 IN ({accounts}) OR account_identification2 IN ({accounts}) "
                     f"OR beneficiary_account_number IN ({accounts})) AND date_created >= '{sql.iso_date(window.start)}' "
                     f"AND date_created < '{sql.iso_date(window.end + timedelta(days=1))}'")
            if amount is not None:
                where += f" AND transaction_value BETWEEN {amount - Decimal('0.01')} AND {amount + Decimal('0.01')}"
            rows = self.mb.query(self.db, f"SELECT {IB_COLUMNS} FROM {table} WHERE {where} "
                                          f"ORDER BY date_created DESC LIMIT {int(self.limit)}")
            for row in rows:
                status, detail = ib_reading(row.get("transaction_status"), row.get("is_txn_reversed"))
                mine = {account_key(row.get("account_identification1")), account_key(row.get("account_identification2"))}
                out.append(Txn(
                    id="", source="ib", channel=label, when=parse_timestamp(row.get("date_created")),
                    amount=to_amount(row.get("transaction_value")), currency=None,
                    account=trim_to_none(row.get("account_identification1")), status=status, detail=detail,
                    counterparty=trim_to_none(row.get("beneficiary_account_number")),
                    reference=trim_to_none(row.get("transaction_reference")),
                    trace=trim_to_none(row.get("transaction_system_reference")),
                    raw={"table": table, "client_is_beneficiary": account_key(account) not in mine},
                ))
        out.sort(key=lambda t: t.when or datetime.min, reverse=True)
        return out


# ---- Core banking (standby) -------------------------------------------------------------------------------
ACCOUNT_SQL = """SELECT X.IACLINK_INTERNAL_ACNUM, A.ACNTS_CURR_CODE
FROM NMBPRODIDC.IACLINK X
LEFT JOIN NMBPRODIDC.ACNTS A ON (A.ACNTS_INTERNAL_ACNUM = X.IACLINK_INTERNAL_ACNUM)
WHERE X.IACLINK_ACTUAL_ACNUM = '{account}'"""
POSTINGS_SQL = """SELECT S.TRAN_DATE_OF_TRAN, S.TRAN_AMOUNT, S.TRAN_CURR_CODE, S.TRAN_DB_CR_FLG, S.TRAN_NARR_DTL1,
    S.TRAN_BATCH_NUMBER, S.TRAN_BATCH_SL_NUM
FROM {table} S
JOIN NMBPRODIDC.IACLINK X ON X.IACLINK_ENTITY_NUM = S.TRAN_ENTITY_NUM AND X.IACLINK_INTERNAL_ACNUM = S.TRAN_INTERNAL_ACNUM
WHERE S.TRAN_ENTITY_NUM = 1
  AND X.IACLINK_ACTUAL_ACNUM = '{account}'
  AND S.TRAN_DATE_OF_TRAN BETWEEN TO_DATE('{start}','DD-MM-YYYY') AND TO_DATE('{end}','DD-MM-YYYY'){amount}
ORDER BY S.TRAN_DATE_OF_TRAN DESC"""


class CoreBanking:
    def __init__(self, mb: MetabaseClient, db: int, tran_pattern: str = "NMBPRODIDC.TRAN{year}", limit: int = 60) -> None:
        self.mb, self.db, self.tran_pattern, self.limit = mb, db, tran_pattern, limit

    def account(self, account: str) -> dict[str, Any]:
        number = sql.digits(account_forms(account)[-1] if account_forms(account) else account)
        rows = self.mb.query(self.db, ACCOUNT_SQL.replace("{account}", sql.oracle_text(number)))
        if not rows:
            return {"account_number": number, "exists": False}
        return {"account_number": number, "exists": True,
                "currency": currency_name(rows[0].get("ACNTS_CURR_CODE"))}

    def postings(self, account: str, window: Window, amount: Optional[Decimal] = None) -> list[Txn]:
        number = sql.digits(account_forms(account)[-1] if account_forms(account) else account)
        clause = (f"\n  AND S.TRAN_AMOUNT BETWEEN {amount - Decimal('0.01')} AND {amount + Decimal('0.01')}"
                  if amount is not None else "")
        rows: list[dict[str, Any]] = []
        for year in range(window.start.year, window.end.year + 1):
            table = self.tran_pattern.replace("{year}", str(year))
            rows += self.mb.query(self.db, POSTINGS_SQL.replace("{table}", table)
                                  .replace("{account}", sql.oracle_text(number))
                                  .replace("{start}", sql.oracle_date(window.start))
                                  .replace("{end}", sql.oracle_date(window.end)).replace("{amount}", clause))
        out = []
        for row in rows[: self.limit]:
            flag = str(row.get("TRAN_DB_CR_FLG") or "").strip().upper()
            narration = trim_to_none(row.get("TRAN_NARR_DTL1")) or ""
            out.append(Txn(
                id="", source="core", channel="core banking posting", when=parse_timestamp(row.get("TRAN_DATE_OF_TRAN")),
                amount=to_amount(row.get("TRAN_AMOUNT")), currency=currency_name(row.get("TRAN_CURR_CODE")),
                account=number, status=POSTED,
                detail=f"{'debit' if flag == 'D' else 'credit' if flag == 'C' else flag or '?'}: {narration[:120]}",
                debit=True if flag == "D" else False if flag == "C" else None,
                reference=f"batch {row.get('TRAN_BATCH_NUMBER')}/{row.get('TRAN_BATCH_SL_NUM')}",
            ))
        out.sort(key=lambda t: t.when or datetime.min, reverse=True)
        return out


REVERSAL_WORDS = ("REV", "REFUND", "RETURN", "CANCEL")


def debit_check(postings: list[Txn], amount: Optional[Decimal], day: Optional[date]) -> dict[str, Any]:
    """Was the client debited this amount around this day, and did the same amount come back?"""
    if amount is None:
        return {"debited": "unknown", "why": "no amount to look for"}
    debits = [p for p in postings if p.debit and p.same_amount(amount)
              and (day is None or p.when is None or abs((p.when.date() - day).days) <= 1)]
    if not debits:
        return {"debited": "no", "why": f"no debit of {amount:,.2f} on the account within a day of the transaction"}
    first_debit = min(debits, key=lambda p: p.when or datetime.max)
    out: dict[str, Any] = {"debited": "yes", "debits": len(debits), "debit": first_debit.detail,
                           "debit_on": format_timestamp(first_debit.when)}
    credits = [p for p in postings if p.debit is False and p.same_amount(amount)
               and (first_debit.when is None or p.when is None
                    or first_debit.when.date() <= p.when.date() <= first_debit.when.date() + timedelta(days=7))]
    named = [p for p in credits if any(w in p.detail.upper() for w in REVERSAL_WORDS)]
    if named or credits:
        refund = (named or credits)[0]
        out.update(refunded="yes" if named else "possibly", refund=refund.detail, refund_on=format_timestamp(refund.when))
        if not named:
            out["why"] = "a credit of the same amount followed, but its narration does not say it is a reversal"
    else:
        out["refunded"] = "no"
    return out
