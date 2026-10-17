"""Where a failed transaction can be seen, through Metabase, read-only:

- POST OFFICE (Postilion, SQL Server) post_tran_leg (the bank's own report query; or post_tran, POSTILION_TABLE):
  card and switch traffic (ZIPIT, bank-to-wallet and wallet-to-bank, POS, ATM, airtime bought with a card).
  Response code, reversal flag, RRN and STAN; with post_tran_leg also the source and sink nodes (which name the
  transaction: "Zipit Send - Us on Them") and the merchant.
- CONECT 2.0 and BILLERS (PostgreSQL): NMB Connect / USSD / app transactions, Zimswitch payments, and the biller
  tables (Econet, NetOne, Telecel airtime; ZESA, City of Harare, offline billers such as ZIMRA).
- IB (MySQL): internet banking RTGS and internal transfers.
- STANDBY DB (Oracle standby of core banking): the postings on the account (TRAN<year>), to see whether the client
  was actually debited and whether the money came back; and, for a switch transaction Intellect declined, Intellect's
  own error for it (the ASI interface log, ASIERRLOG by RRN). Never the live core banking system.

Every source returns Txn objects with one reading (succeeded, declined, reversed, ...) worked out here, in code,
from the system's own codes; the model reads these readings, it does not invent them. Ported from the Java
investigators (PostilionTransactionInvestigator, NmbConnectTransactionInvestigator, IbTransactionInvestigator,
FailedTransactionInvestigator), with the fixes the review found (cents, reversal flag "2", the 25-row cut before
the amount filter).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient, MetabaseError
from .normalize import (
    account_forms,
    account_key,
    mobile_key,
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
# The switch's own explanation, on both tables: extended_rsp_code (Postilion's 4-character code, e.g. P050) and
# structured_data_rsp (key/value pairs; see switch_says for the few keys that are read).
SWITCH_SAYS_COLUMNS = "extended_rsp_code, structured_data_rsp"
POSTILION_COLUMNS = ("post_tran_id, message_type, tran_type, tran_completed, rsp_code_rsp, abort_rsp_code, "
                     "tran_reversed, prev_post_tran_id, tran_amount_req, tran_currency_code, datetime_tran_local, "
                     "retrieval_reference_nr, system_trace_audit_nr, from_account_id, to_account_id, "
                     + SWITCH_SAYS_COLUMNS)
# The bank's Postilion report query reads post_tran_leg, one row per leg, with the nodes and the merchant. The
# columns that query reads are used here, and the switch's explanation. (PAN is left out on purpose: no lookup needs
# the card number.)
LEG_COLUMNS = ("datetime_req, tran_nr, tran_type, message_type, rsp_code_rsp, source_node_name, sink_node_name, "
               "system_trace_audit_nr, retrieval_reference_nr, tran_currency_code, settle_amount_req, "
               "from_account_id, to_account_id, card_acceptor_name_loc, tran_reversed, " + SWITCH_SAYS_COLUMNS)
POSTILION_TABLES = ("post_tran_leg", "post_tran")

# Response codes for which the switch processed the request and the answer is a business one (the bank's Postilion
# success-rate report counts these as successful processing): approved, or declined for a reason on the client's
# side such as 51 insufficient funds or 55 wrong PIN. Any other non-approval code (other than the no-answer codes
# above) is a technical failure on a node or link, so whether the client was debited must be checked.
SWITCH_PROCESSED_CODES = frozenset({
    "00", "51", "55", "61", "12", "13", "41", "42", "45", "54", "56", "57", "58", "59", "62", "65", "67", "75",
    "30", "38", "14", "39", "01", "40", "05", "43",
})

# What a switch transaction is, as the bank names it: (tran type, message type, source node, sink node). From the
# bank's Postilion transaction-type map. "Us on Us": our client at our device; "Them on Us": another bank's client at
# our device; "Us on Them": our client at another bank's device.
_TXN_MAP = [
    (0, 200, "EFTHostedSrc", "NmbIntellect", "POS Purchase - Us on Us"),
    (0, 200, "EFTHostedSrc", "NmbZswAcq", "POS Purchase - Them on Us"),
    (0, 200, "MatsimbaSrc", "NmbIntellect", "POS Purchase - Us on Us"),
    (0, 200, "MatsimbaSrc", "NmbZswAcq", "POS Purchase - Them on Us"),
    (0, 200, "NmbZswIss", "NmbIntellect", "POS Purchase - Us on Them"),
    (0, 200, "PosCloudSrc", "NmbIntellect", "POS Purchase - Us on Us"),
    (0, 200, "PosCloudSrc", "NmbZswAcq", "POS Purchase - Them on Us"),
    (0, 220, "EFTHostedSrc", "NmbIntellect", "POS Purchase - Them on Us"),   # 0220, as the bank's map has it
    (0, 220, "MatsimbaSrc", "NmbIntellect", "POS Purchase - Them on Us"),
    (0, 320, "NmbTellerSrc", "NmbIntellect", "Pin Issue"),
    (0, 320, "NmbVNodeSrc", "NmbIntellect", "Pin Issue"),
    (0, 420, "EFTHostedSrc", "NmbIntellect", "POS Purchase Reversal - Us on Us"),
    (0, 420, "EFTHostedSrc", "NmbZswAcq", "POS Purchase Reversal - Them on Us"),
    (0, 420, "NmbZswIss", "NmbIntellect", "POS Purchase Reversal - Us on Them"),
    (1, 200, "NmbAtmSrc", "NmbIntellect", "Withdrawal - Us on Us"),
    (1, 200, "NmbAtmSrc", "NmbZswAcq", "Withdrawal - Them on Us"),
    (1, 200, "NmbZswIss", "NmbIntellect", "Withdrawal - Us on Them"),
    (1, 420, "NmbAtmSrc", "NmbIntellect", "Withdrawal Reversal - Us on Us"),
    (9, 200, "EFTHostedSrc", "NmbZswAcq", "POS Purchase + Cash - Them on Us"),
    (9, 200, "NmbZswIss", "NmbIntellect", "POS Purchase + Cash - Us on Them"),
    (9, 200, "PosCloudSrc", "NmbZswAcq", "POS Purchase + Cash - Them on Us"),
    (27, 200, "NmbEcocashSc", "NmbIntellect", "Wallet to Bank"),
    (27, 200, "NmbZswIss", "NmbIntellect", "Zipit Receive"),
    (40, 200, "NmbAtmSrc", "NmbIntellect", "Internal Transfer"),
    (40, 200, "NmbEcocashSc", "NmbIntellect", "Bank to Wallet"),
    (42, 200, "NmbVNodeSrc", "NmbZswAcq", "Zipit Send"),
    (50, 200, "NmbZswIss", "NmbIntellect", "Zipit Send"),
    (50, 420, "NmbZswIss", "NmbIntellect", "Zipit Send Reversal"),
    (92, 600, "NmbTellerSrc", "NmbIntellect", "Change Pin"),
    (0, 200, "EFTHostedSrc", "EftHostedSnk", "POS Ecocash Acquiring"),
    (0, 200, "PosCloudSrc", "EftHostedSnk", "POS Ecocash Acquiring"),
    (0, 420, "EFTHostedSrc", "EftHostedSnk", "POS Ecocash Acquiring Reversal"),
    (1, 200, "NmbAtmSrc", "VisaAcq", "ATM Visa Acquiring"),
    (39, 100, "NmbAtmSrc", "VisaAcq", "ATM Visa Acquiring"),
]
TXN_NAMES = {(t, m, src.lower(), snk.lower()): name for t, m, src, snk, name in _TXN_MAP}


def _int_code(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def txn_name(tran_type: Any, message_type: Any, source: Any, sink: Any) -> Optional[str]:
    """The bank's name for a switch transaction, from its type, message type and nodes (node names in any case)."""
    key = (_int_code(tran_type), _int_code(message_type), str(source or "").strip().lower(),
           str(sink or "").strip().lower())
    return TXN_NAMES.get(key)  # type: ignore[arg-type]


# structured_data_rsp also carries the sender's and receiver's names, national ID, mobile number and e-mail. Only
# these keys are read; everything else in it is dropped here and never reaches the model or the log.
SAYS_REASON_KEYS = ("Reason", "Postilion:DeclineReason")
SAYS_EXTENDED_KEYS = ("Postilion:ExtendedResponseCode", "Postilion:ExtendedRspCode")
SAYS_KEYS = frozenset(SAYS_REASON_KEYS + SAYS_EXTENDED_KEYS + ("EcocashErrorCode", "EcocashErrorDesc", "ReceiverBank"))


def _item(text: str, i: int) -> tuple[str, int]:
    """One length-prefixed item: a digit n, then an n-digit length, then that many characters."""
    if i >= len(text) or not text[i].isdigit() or text[i] == "0":
        raise ValueError("no length prefix")
    n = int(text[i])
    size = text[i + 1:i + 1 + n]
    if len(size) != n or not size.isdigit():
        raise ValueError("bad length")
    end = i + 1 + n + int(size)
    if end > len(text):
        raise ValueError("length runs past the end")
    return text[i + 1 + n:end], end


def parse_structured(text: Optional[str]) -> Optional[dict[str, str]]:
    """Postilion structured data ('<n><length><key><n><length><value>', repeated) as a dict; None when the text is
    not in that format."""
    text = (text or "").rstrip()
    out: dict[str, str] = {}
    i = 0
    try:
        while i < len(text):
            key, i = _item(text, i)
            value, i = _item(text, i)
            out[key] = value
    except ValueError:
        return None
    return out or None


def _said(value: str, limit: int = 80) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[:limit] + "…"


# Postilion's advanced limits (structured_data_rsp key RemainingAdvancedLimits): an XML list of the limits that apply,
# <Limits><Limit><LimitName/><Period/><Association/><OnlineTotalLimit/><RemOnlineTotalLimit/>... per limit. On a 61
# or 65 the switch declined itself, they are the limit that stopped the client (read with a pattern, not an XML
# parser: the value is the switch's own and only leaf elements are needed).
LIMITS_KEY = "RemainingAdvancedLimits"
LIMIT_CODES = ("61", "65")
_LIMIT_BLOCK = re.compile(r"<Limit\b[^>]*>(.*?)</Limit>", re.S)
_LEAF = re.compile(r"<([A-Za-z][\w]*)\b[^>]*>([^<]*)</\1>")
LIMIT_LABELS = (  # what is shown, in this order, as Postilion holds it
    ("OnlinePerTransactionAmountLimit", "per transaction"), ("OnlineTotalLimit", "total"),
    ("RemOnlineTotalLimit", "remaining"), ("OnlineNrTransLimit", "number allowed"),
    ("RemOnlineNrTransLimit", "number remaining"), ("OverallTotalLimit", "overall total"),
    ("RemOverallTotalLimit", "overall remaining"), ("OverallNrTransLimit", "overall number allowed"),
    ("RemOverallNrTransLimit", "overall number remaining"),
)


def advanced_limits(xml: Optional[str]) -> list[dict[str, str]]:
    """Each <Limit> in Postilion's RemainingAdvancedLimits as {element: text} (non-empty leaves only)."""
    out = []
    for block in _LIMIT_BLOCK.findall((xml or "")[:20000]):
        fields = {tag: re.sub(r"\s+", " ", text).strip() for tag, text in _LEAF.findall(block) if text.strip()}
        if fields:
            out.append(fields)
    return out[:10]


LIMIT_CURRENCY = re.compile(r"\b(USD|ZWG|ZIG|ZWL|ZAR)", re.I)


def _cents_text(value: Optional[str]) -> Optional[Decimal]:
    try:
        return (Decimal(str(value).strip()) / 100).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _count(value: Optional[str]) -> Optional[int]:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 6 else None  # 12-digit values mean "no limit"


def limit_currency(limit: dict[str, str], fallback: Optional[str] = None) -> Optional[str]:
    found = LIMIT_CURRENCY.search(limit.get("LimitName", ""))
    return currency_name(found.group(1)) if found else fallback


def describe_limit(limit: dict[str, str], currency: Optional[str] = None) -> str:
    """One Postilion limit in words. Amounts are held in cents; counts of 12 digits mean no limit."""
    cur = limit_currency(limit, currency)
    head = limit.get("LimitName", "limit")
    extra = ", ".join(limit[k] for k in ("Period", "Association") if limit.get(k))
    values = []
    for key, label in LIMIT_LABELS:
        if not limit.get(key):
            continue
        if "Nr" in key:
            n = _count(limit[key])
            if n is not None:
                values.append(f"{label} {n}")
        else:
            amount = _cents_text(limit[key])
            if amount is not None:
                values.append(f"{label} {money(amount, cur)}")
    return f"{head}" + (f" ({extra})" if extra else "") + (f": {', '.join(values)}" if values else "")


def limit_hit(limits: list[dict[str, str]], amount: Optional[Decimal], currency: Optional[str]) -> Optional[str]:
    """Which of Postilion's limits stopped a transaction of this amount, in words; None when none clearly did.
    Only limits in the transaction's currency (or with none in their name) are considered."""
    for limit in limits:
        cur = limit_currency(limit)
        if cur and currency and cur != currency.upper():
            continue
        cur = cur or currency
        name, period = limit.get("LimitName", "a limit"), limit.get("Period")
        per = f" per {period}" if period else ""
        left_n = _count(limit.get("RemOnlineNrTransLimit") or limit.get("RemOverallNrTransLimit"))
        allowed_n = _count(limit.get("OnlineNrTransLimit") or limit.get("OverallNrTransLimit"))
        if left_n == 0:
            return f"Postilion's limit {name}: {allowed_n if allowed_n is not None else 'the'} transactions allowed{per}, none left"
        if amount is None:
            continue
        each = _cents_text(limit.get("OnlinePerTransactionAmountLimit") or limit.get("OverallPerTransactionAmountLimit"))
        if each is not None and amount > each:
            return f"Postilion's limit {name}: at most {money(each, cur)} per transaction"
        total = _cents_text(limit.get("OnlineTotalLimit") or limit.get("OverallTotalLimit"))
        left = _cents_text(limit.get("RemOnlineTotalLimit") or limit.get("RemOverallTotalLimit"))
        if left is not None and amount > left:
            return (f"Postilion's limit {name}: {money(total, cur) if total is not None else 'a total'}{per}, "
                    f"{money(left, cur)} left")
    return None


def switch_fields(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the switch itself said about a transaction, beyond the 2-digit response code: reason, ecocash_code,
    ecocash_desc, extended (Postilion's extended response code), receiving_bank, limits (Postilion's advanced limits).
    From the first leg that has each."""
    found: dict[str, str] = {}
    extended = None
    limits: list[dict[str, str]] = []
    for row in rows:
        extended = extended or trim_to_none(row.get("extended_rsp_code"))
        for key, value in (parse_structured(row.get("structured_data_rsp")) or {}).items():
            if key == LIMITS_KEY and not limits:
                limits = advanced_limits(value)
            elif key in SAYS_KEYS and value.strip() and key not in found:
                found[key] = _said(value)
    out: dict[str, Any] = {}
    if limits:
        out["limits"] = limits
    reason = next((found[k] for k in SAYS_REASON_KEYS if k in found), None)
    if reason:
        out["reason"] = reason
    if found.get("EcocashErrorCode"):
        out["ecocash_code"] = found["EcocashErrorCode"]
    if found.get("EcocashErrorDesc"):
        out["ecocash_desc"] = found["EcocashErrorDesc"]
    extended = extended or next((found[k] for k in SAYS_EXTENDED_KEYS if k in found), None)
    if extended:
        out["extended"] = _said(extended, 8)
    if found.get("ReceiverBank"):
        out["receiving_bank"] = found["ReceiverBank"]
    return out


def switch_says(rows: list[dict[str, Any]], with_limits: bool = False, currency: Optional[str] = None) -> list[str]:
    """switch_fields in words, for the transaction's detail (Postilion's limits only when asked: on a limit decline)."""
    fields = switch_fields(rows)
    out = []
    if fields.get("reason"):
        out.append(f'the switch says "{fields["reason"]}"')
    code, desc = fields.get("ecocash_code"), fields.get("ecocash_desc")
    if code or desc:
        out.append("EcoCash error" + (f" {code}" if code else "") + (f': "{desc}"' if desc else ""))
    if fields.get("extended"):
        out.append(f"Postilion extended response code {fields['extended']}")
    if fields.get("receiving_bank"):
        out.append(f"receiving bank {fields['receiving_bank']}")
    if with_limits and fields.get("limits"):
        out.append("Postilion's limits on the card: " + "; ".join(describe_limit(x, currency) for x in fields["limits"][:4]))
    return out


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
    reason: Optional[str] = None  # the system's own words for a decline (switch reason, core banking error)
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
    """POST OFFICE. Amounts are stored in cents.

    table post_tran_leg (the bank's report query): one row per leg with the source and sink nodes and the merchant;
    the client-facing leg only (tran_postilion_originated = '0'). Fully reversed transactions are kept, unlike in
    the report: a reversal is often the answer to a ticket.
    table post_tran: the columns the Java investigator read (completion and abort codes, no nodes); a transaction
    can appear once per leg."""

    def __init__(self, mb: MetabaseClient, db: int, limit: int = 80, table: str = "post_tran") -> None:
        if table not in POSTILION_TABLES:
            raise ValueError(f"Postilion table must be one of {POSTILION_TABLES}")
        self.mb, self.db, self.limit, self.table = mb, db, limit, table
        legs = table == "post_tran_leg"
        self._when = "datetime_req" if legs else "datetime_tran_local"
        self._cents = "settle_amount_req" if legs else "tran_amount_req"

    def by_account(self, account: str, window: Window, amount: Optional[Decimal] = None) -> list[Txn]:
        accounts = _accounts_in(account)
        where = [f"(from_account_id IN ({accounts}) OR to_account_id IN ({accounts}))",
                 f"{self._when} >= '{sql.iso_date(window.start)}'",
                 f"{self._when} < '{sql.iso_date(window.end + timedelta(days=1))}'",
                 "tran_type NOT LIKE '3%'" if self.table == "post_tran_leg"
                 else f"tran_type NOT IN ('{INQUIRY_TYPES[0]}', '{INQUIRY_TYPES[1]}')"]
        if amount is not None:  # filter in SQL (cents), so a busy account cannot push the right row off the list
            cents = int((amount * 100).to_integral_value())
            where.append(f"{self._cents} BETWEEN {cents - 1} AND {cents + 1}")
        return self._read(" AND ".join(where))

    def by_reference(self, reference: str) -> list[Txn]:
        """By RRN (the STAN repeats across transactions, so it is not used on its own)."""
        return self._read(f"retrieval_reference_nr = '{sql.mysql_text(reference)}'")

    def _read(self, where: str) -> list[Txn]:
        if self.table == "post_tran_leg":
            query = (f"SELECT TOP {int(self.limit)} {LEG_COLUMNS}\nFROM post_tran_leg WITH (NOLOCK)\n"
                     f"WHERE {where} AND tran_postilion_originated = '0'\nORDER BY datetime_req DESC")
        else:
            query = (f"SELECT TOP {int(self.limit)} {POSTILION_COLUMNS}\nFROM post_tran WITH (NOLOCK)\n"
                     f"WHERE {where}\nORDER BY datetime_tran_local DESC")
        return postilion_transactions(self.mb.query(self.db, query))


def _flag(value: Any) -> int:
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "t", "y", "yes"):
        return 1
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def _cents(row: dict[str, Any]) -> Optional[Decimal]:
    return to_amount(row["tran_amount_req"] if row.get("tran_amount_req") is not None else row.get("settle_amount_req"))


def _message_type(row: dict[str, Any]) -> str:
    text = str(row.get("message_type") or "").strip()
    return text.zfill(4) if text.isdigit() else text  # 420 and "0420" are the same message type


def postilion_transactions(rows: list[dict[str, Any]]) -> list[Txn]:
    """One Txn per switch transaction: legs are merged, reversal advices (04xx) are folded into the original.
    Works on post_tran rows and on post_tran_leg rows (which carry nodes and the merchant, not completion)."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        key = (trim_to_none(row.get("retrieval_reference_nr")) or str(row.get("post_tran_id") or row.get("tran_nr")),
               trim_to_none(row.get("system_trace_audit_nr")),
               _cents(row))
        groups.setdefault(key, []).append(row)
    out: list[Txn] = []
    for members in groups.values():
        originals = [r for r in members if not _message_type(r).startswith("04")] or members
        advices = [r for r in members if _message_type(r).startswith("04")]
        row = originals[0]
        rsp = trim_to_none(row.get("rsp_code_rsp")) or ""
        reversed_flag = max(_flag(r.get("tran_reversed")) for r in members)  # 0 no, 1 partial, 2 full
        # post_tran_leg has no completion flag: the response code is the answer there.
        completed = all(_flag(r.get("tran_completed")) if "tran_completed" in r else True for r in originals)
        abort = trim_to_none(row.get("abort_rsp_code"))
        cents = _cents(row)
        detail = f"response {rsp or '-'} ({rsp_meaning(rsp)})"
        said = switch_fields(originals + advices)
        says = switch_says(originals + advices, with_limits=rsp in LIMIT_CODES,
                           currency=currency_name(row.get("tran_currency_code")))
        if says:
            detail += "; " + "; ".join(says)
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
        elif rsp in SWITCH_PROCESSED_CODES:
            status = DECLINED
            detail += "; the switch processed it, so this is a business decline"
        else:
            status = FAILED  # not a business answer: a node or link failed, so the client may have been debited
            detail += "; a technical failure (not a business answer from the bank or issuer)"
        tran_type = str(row.get("tran_type") or "").strip()
        tran_type = tran_type.zfill(2) if tran_type.isdigit() else tran_type
        source_node, sink_node = trim_to_none(row.get("source_node_name")), trim_to_none(row.get("sink_node_name"))
        name = txn_name(tran_type, _message_type(row), source_node, sink_node)
        channel = name or f"card/switch {TRAN_TYPES.get(tran_type, f'type {tran_type}')}"
        if source_node or sink_node:
            detail += f"; via {source_node or '?'} to {sink_node or '?'}"
        merchant = trim_to_none(row.get("card_acceptor_name_loc"))
        if merchant:
            detail += "; at " + re.sub(r"\s+", " ", merchant)  # the merchant name pads with runs of spaces
        accounts = [trim_to_none(row.get("from_account_id")), trim_to_none(row.get("to_account_id"))]
        amount = (cents / 100).quantize(Decimal("0.01")) if cents is not None else None
        currency = currency_name(row.get("tran_currency_code"))
        hit = limit_hit(said.get("limits") or [], amount, currency) if rsp in LIMIT_CODES else None
        if hit:
            detail += f"; the limit it hit: {hit}"
        out.append(Txn(
            id="", source="postilion", channel=channel,
            when=parse_timestamp(row.get("datetime_tran_local") or row.get("datetime_req")),
            amount=amount, currency=currency, account=accounts[0] or accounts[1], status=status,
            detail=detail, counterparty=accounts[1] if accounts[0] else None,
            reference=trim_to_none(row.get("retrieval_reference_nr")), trace=trim_to_none(row.get("system_trace_audit_nr")),
            reversal=reversal, reason=said.get("reason") or said.get("ecocash_desc") or hit,
            raw={"legs": len(members), "reversal_advices": len(advices), "rsp": rsp, "sink": sink_node,
                 "limits": said.get("limits") if rsp in LIMIT_CODES else None, "limit_hit": hit},
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


# ---- EcoCash links (Ecocash Reg) ---------------------------------------------------------------------------
# The bank's log of EcoCash wallet links: each row links a wallet (msisdn, 263...) to an account (account_id) in a
# currency (840 USD, 924 ZWG). service_type REGISTRATION is the current way of linking, BANKREG the earlier one, and
# DEREGISTRATION removes the link. The latest event for a mobile and currency is the link in force. date_added is
# text (YYYY-MM-DD HH:MM:SS...). The names and ID numbers in the table are not read.
LINK_EVENTS = {"REGISTRATION": "linked", "BANKREG": "linked (earlier method)", "DEREGISTRATION": "delinked"}
ECOCASH_LINK_COLUMNS = "account_id, msisdn, service_type, currency_code, date_added"
TABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


class EcoCashLinks:
    def __init__(self, mb: MetabaseClient, db: int, table: str = "public.ecocash_registration", limit: int = 60) -> None:
        if not TABLE_NAME_RE.fullmatch(table):
            raise ValueError(f"EcoCash registration table must be schema.table: {table!r}")
        self.mb, self.db, self.table, self.limit = mb, db, table, limit

    def _events(self, accounts: list[str], mobiles: list[str]) -> list[dict[str, Any]]:
        where = []
        forms = [f for a in accounts for f in account_forms(a)]
        if forms:
            where.append("TRIM(account_id) IN (" + ", ".join(f"'{sql.digits(f)}'" for f in dict.fromkeys(forms)) + ")")
        keys = [k for k in (mobile_key(m) for m in mobiles) if k]
        if keys:
            numbers = [n for k in dict.fromkeys(keys) for n in ("263" + k, "0" + k, k)]
            where.append("TRIM(msisdn) IN (" + ", ".join(f"'{sql.digits(n)}'" for n in numbers) + ")")
        if not where:
            return []
        return self.mb.query(self.db, f"SELECT {ECOCASH_LINK_COLUMNS}\nFROM {self.table}\nWHERE " + " OR ".join(where)
                             + f"\nORDER BY date_added DESC\nLIMIT {int(self.limit)}")

    def find(self, account: Optional[str] = None, mobile: Optional[str] = None,
             as_of: Optional[date] = None) -> dict[str, Any]:
        """The wallets linked to an account and/or a mobile, each with its link in force and recent history.
        as_of: only events up to that day (closed-ticket replays: the links as they stood then)."""
        rows = self._events([account] if account else [], [mobile] if mobile else [])
        wallets = {mobile_key(r.get("msisdn")) for r in rows} - {None}
        if wallets:  # each wallet's whole history, to see links to other accounts and later delinks
            rows += self._events([], ["0" + w for w in wallets])
        seen, events = set(), []
        for r in rows:
            key = (str(r.get("account_id") or "").strip(), mobile_key(r.get("msisdn")), str(r.get("service_type") or ""),
                   str(r.get("currency_code") or ""), str(r.get("date_added") or ""))
            if key not in seen and key[1] and (as_of is None or key[4][:10] <= as_of.isoformat()):
                seen.add(key)
                events.append(key)
        events.sort(key=lambda e: e[4], reverse=True)  # date_added text sorts by time
        out_wallets = []
        for wallet in sorted({(e[1], e[3]) for e in events}):
            history = [e for e in events if (e[1], e[3]) == wallet]
            latest = history[0]
            kind = latest[2].upper()
            linked = kind != "DEREGISTRATION"
            out_wallets.append({
                "mobile": "0" + wallet[0], "currency": currency_name(wallet[1]) if wallet[1] not in ("", "None") else None,
                "linked_now": linked, "account": latest[0] if linked else None,
                "since" if linked else "delinked_on": _link_day(latest[4]),
                "history": [{"on": _link_day(e[4]), "event": LINK_EVENTS.get(e[2].upper(), e[2]), "account": e[0]}
                            for e in history[:6]],
            })
        out: dict[str, Any] = {"wallets": out_wallets}
        if account:
            key = account_key(account)
            now = [w for w in out_wallets if w["linked_now"] and account_key(w["account"]) == key]
            ever = any(account_key(e[0]) == key for e in events)
            out["account"] = {"account_number": account, "linked_now_to": [w["mobile"] + (f" ({w['currency']})"
                              if w["currency"] else "") for w in now], "ever_linked": ever}
        return out


def _link_day(text: str) -> Optional[str]:
    when = parse_timestamp(text)
    return when.strftime("%d-%m-%Y") if when else (text[:10] or None)


# ---- Core banking (standby) -------------------------------------------------------------------------------
# The account's status and the permission the helpdesk checks for a response 57 (not permitted): ATM use enabled,
# dormant, inoperative, frozen, closed. No name or address is read. (The mobile and internet banking flags are left
# out: closed tickets showed they do not explain EcoCash or ZIPIT failures, and the agent read them as a cause.)
ACCOUNT_FLAGS = {
    "atm_enabled": "ACNTS_ATM_OPERN", "dormant": "ACNTS_DORMANT_ACNT", "inoperative": "ACNTS_INOP_ACNT",
    "debits_frozen": "ACNTS_DB_FREEZED", "credits_frozen": "ACNTS_CR_FREEZED",
}
# Accounts are matched in every form the number may be stored or written in (as given, without leading zeros,
# zero-padded to 14): other systems (the EcoCash link log, the switch) do not always keep core banking's padding.
ACCOUNT_SQL = """SELECT X.IACLINK_ACTUAL_ACNUM, X.IACLINK_INTERNAL_ACNUM, A.ACNTS_CURR_CODE, """ + ", ".join(
    f"A.{c}" for c in ACCOUNT_FLAGS.values()) + """, A.ACNTS_CLOSURE_DATE
FROM NMBPRODIDC.IACLINK X
LEFT JOIN NMBPRODIDC.ACNTS A ON (A.ACNTS_INTERNAL_ACNUM = X.IACLINK_INTERNAL_ACNUM)
WHERE X.IACLINK_ACTUAL_ACNUM IN ({accounts})"""
# A client's accounts by customer number (CIF): ACNTS_CLIENT_NUM, joined to IACLINK for the account number the
# branches use. No name or address is read.
CLIENT_ACCOUNTS_SQL = """SELECT X.IACLINK_ACTUAL_ACNUM, A.ACNTS_CURR_CODE, A.ACNTS_OPENING_DATE, """ + ", ".join(
    f"A.{c}" for c in ACCOUNT_FLAGS.values()) + """, A.ACNTS_CLOSURE_DATE
FROM NMBPRODIDC.ACNTS A
JOIN NMBPRODIDC.IACLINK X ON X.IACLINK_ENTITY_NUM = A.ACNTS_ENTITY_NUM AND X.IACLINK_INTERNAL_ACNUM = A.ACNTS_INTERNAL_ACNUM
WHERE A.ACNTS_ENTITY_NUM = 1
  AND A.ACNTS_CLIENT_NUM = {cif}
  AND ROWNUM <= 25"""
POSTINGS_SQL = """SELECT S.TRAN_DATE_OF_TRAN, S.TRAN_AMOUNT, S.TRAN_CURR_CODE, S.TRAN_DB_CR_FLG, S.TRAN_NARR_DTL1,
    S.TRAN_BATCH_NUMBER, S.TRAN_BATCH_SL_NUM
FROM {table} S
JOIN NMBPRODIDC.IACLINK X ON X.IACLINK_ENTITY_NUM = S.TRAN_ENTITY_NUM AND X.IACLINK_INTERNAL_ACNUM = S.TRAN_INTERNAL_ACNUM
WHERE S.TRAN_ENTITY_NUM = 1
  AND X.IACLINK_ACTUAL_ACNUM IN ({accounts})
  AND S.TRAN_DATE_OF_TRAN BETWEEN TO_DATE('{start}','DD-MM-YYYY') AND TO_DATE('{end}','DD-MM-YYYY'){amount}
ORDER BY S.TRAN_DATE_OF_TRAN DESC"""


# Intellect's switch interface (ASI) logs why it declined a request from Postilion: asiinnp is the request as received
# (field 37 is the RRN), asierrlog the error against it, e.g. "Transaction Limit for Cash Withdrawals exceeded for the
# month Limit of 300". The bank's own lookup, by RRN and the interface's business date.
ASI_ERRORS_SQL = """SELECT H.ERR_LOG_DATETIME, H.ERR_PGM_ID, H.ERR_DESC
FROM NMBPRODIDC.ASIERRLOG H
JOIN NMBPRODIDC.ASIINNP A ON H.ERR_ENTITY_NUM = A.ASIIN_ENTITY_NUM AND H.ERR_IN_BRN_CODE = A.ASIIN_BRN_CODE
  AND H.ERR_IN_DATE = A.ASIIN_DATE AND H.ERR_IN_SERIAL = A.ASIIN_SERIAL
WHERE A.ASIIN_ENTITY_NUM = '1'
  AND A.ASIIN_FIELD_037 = '{rrn}'
  AND A.ASIIN_DATE BETWEEN TO_DATE('{start}','DD-MM-YYYY') AND TO_DATE('{end}','DD-MM-YYYY')
ORDER BY H.ERR_LOG_DATETIME"""
# Intellect's own answer to each message the switch sent it for an RRN: asiinnp is the request (the original, or its
# reversal), asioutnp the reply Intellect sent back (field 39). Postilion's "reversed" flag only says the switch sent a
# reversal; this says whether Intellect accepted it. The serial is one sequence per branch code across both ASI
# servers, so it gives the order the messages reached Intellect in (their clocks differ). The raw message is not read.
ASI_ANSWERS_SQL = """SELECT A.ASIIN_MTI, A.ASIIN_BRN_CODE, A.ASIIN_SERIAL, O.ASIOUT_MTI, O.ASIOUT_FIELD_039,
  (SELECT MAX(H.ERR_DESC) FROM NMBPRODIDC.ASIERRLOG H WHERE H.ERR_ENTITY_NUM = A.ASIIN_ENTITY_NUM
     AND H.ERR_IN_BRN_CODE = A.ASIIN_BRN_CODE AND H.ERR_IN_DATE = A.ASIIN_DATE AND H.ERR_IN_SERIAL = A.ASIIN_SERIAL) AS ERR_DESC
FROM NMBPRODIDC.ASIINNP A
LEFT JOIN NMBPRODIDC.ASIOUTNP O ON O.ASIOUT_ENTITY_NUM = A.ASIIN_ENTITY_NUM AND O.ASIOUT_BRN_CODE = A.ASIIN_ASIOUT_BRN_CODE
  AND O.ASIOUT_DATE = A.ASIIN_ASIOUT_DATE AND O.ASIOUT_SERIAL = A.ASIIN_ASIOUT_SERIAL
WHERE A.ASIIN_ENTITY_NUM = '1'
  AND A.ASIIN_FIELD_037 = '{rrn}'
  AND A.ASIIN_DATE BETWEEN TO_DATE('{start}','DD-MM-YYYY') AND TO_DATE('{end}','DD-MM-YYYY')
ORDER BY A.ASIIN_BRN_CODE, A.ASIIN_SERIAL"""
RRN_RE = re.compile(r"[0-9A-Za-z]{6,32}")


def _account_list(account: str) -> str:
    """The account in every form it may be stored in, as an SQL list of literals (digits only)."""
    forms = account_forms(account)
    if not forms:
        raise sql.UnsafeValue("not an account number")
    return ", ".join(f"'{sql.digits(f)}'" for f in forms)


def account_status(row: dict[str, Any]) -> dict[str, Any]:
    """Currency, status flags and restrictions from an ACNTS row."""
    status: dict[str, Any] = {key: yes_no(row.get(column)) for key, column in ACCOUNT_FLAGS.items()}
    closed = parse_timestamp(row.get("ACNTS_CLOSURE_DATE"))
    status["closed_on"] = closed.strftime("%d-%m-%Y") if closed else None
    return {"currency": currency_name(row.get("ACNTS_CURR_CODE")), "status": status,
            "restrictions": restrictions(status)}


def yes_no(value: Any) -> Optional[bool]:
    """A core banking flag (1/0, Y/N); None when it is empty or something else."""
    text = (trim_to_none(value) or "").lower()
    if text in ("1", "y", "yes", "t", "true"):
        return True
    if text in ("0", "n", "no", "f", "false"):
        return False
    return None


def restrictions(status: dict[str, Any]) -> list[str]:
    """What on the account stops a debit or an ATM / card transaction, in words."""
    out = []
    if status.get("closed_on"):
        out.append(f"the account was closed on {status['closed_on']}")
    if status.get("dormant"):
        out.append("the account is dormant")
    if status.get("inoperative"):
        out.append("the account is inoperative")
    if status.get("debits_frozen"):
        out.append("debits are frozen on the account")
    if status.get("atm_enabled") is False:
        out.append("ATM use is not enabled on the account")
    return out


class CoreBanking:
    def __init__(self, mb: MetabaseClient, db: int, tran_pattern: str = "NMBPRODIDC.TRAN{year}", limit: int = 60) -> None:
        if not sql.TRAN_PATTERN_RE.fullmatch(tran_pattern):
            raise ValueError(f"TRAN table pattern must be schema.table with {{year}}: {tran_pattern!r}")
        self.mb, self.db, self.tran_pattern, self.limit = mb, db, tran_pattern, limit

    def account(self, account: str) -> dict[str, Any]:
        number = sql.digits(account_forms(account)[-1] if account_forms(account) else account)
        rows = self.mb.query(self.db, ACCOUNT_SQL.replace("{accounts}", _account_list(account)))
        if not rows:
            return {"account_number": number, "exists": False}
        actual = trim_to_none(rows[0].get("IACLINK_ACTUAL_ACNUM")) or number
        return {"account_number": actual, "exists": True, **account_status(rows[0])}

    def client_accounts(self, cif: str) -> list[dict[str, Any]]:
        """The accounts core banking holds for a customer number (CIF), with currency and status."""
        number = sql.integer(sql.digits(cif))
        rows = self.mb.query(self.db, CLIENT_ACCOUNTS_SQL.replace("{cif}", number))
        out = []
        for row in rows:
            account = trim_to_none(row.get("IACLINK_ACTUAL_ACNUM"))
            if account:
                opened = parse_timestamp(row.get("ACNTS_OPENING_DATE"))
                out.append({"account_number": account, "opened_on": opened.strftime("%d-%m-%Y") if opened else None,
                            **account_status(row)})
        out.sort(key=lambda a: (bool(a["status"].get("closed_on")), a["account_number"]))  # open accounts first
        return out

    def switch_errors(self, rrn: str, day: date) -> list[str]:
        """Intellect's own error messages for a switch request it declined, by RRN, around the day (the interface's
        business date can lag or lead the switch's calendar day)."""
        rrn = (rrn or "").strip()
        if not RRN_RE.fullmatch(rrn):
            raise sql.UnsafeValue("not an RRN")
        rows = self.mb.query(self.db, ASI_ERRORS_SQL.replace("{rrn}", sql.oracle_text(rrn))
                             .replace("{start}", sql.oracle_date(day - timedelta(days=1)))
                             .replace("{end}", sql.oracle_date(day + timedelta(days=1))))
        out: list[str] = []
        for row in rows:
            text = re.sub(r"\s+", " ", str(row.get("ERR_DESC") or "")).strip()
            if text and text not in out:
                out.append(text[:200])
        return out

    def switch_answers(self, rrn: str, day: date) -> list[dict[str, Any]]:
        """What Intellect answered to each message the switch sent it for an RRN (original and reversal), around the
        day, in the order they reached it: mti, branch, serial, rc (Intellect's field 39, None when it sent no reply)
        and error (its interface error for that message)."""
        rrn = (rrn or "").strip()
        if not RRN_RE.fullmatch(rrn):
            raise sql.UnsafeValue("not an RRN")
        rows = self.mb.query(self.db, ASI_ANSWERS_SQL.replace("{rrn}", sql.oracle_text(rrn))
                             .replace("{start}", sql.oracle_date(day - timedelta(days=1)))
                             .replace("{end}", sql.oracle_date(day + timedelta(days=1))))
        out = []
        for row in rows:
            error = re.sub(r"\s+", " ", str(row.get("ERR_DESC") or "")).strip()
            mti = str(row.get("ASIIN_MTI") or "").strip()
            out.append({"mti": mti.zfill(4) if mti.isdigit() else mti, "branch": trim_to_none(row.get("ASIIN_BRN_CODE")),
                        "serial": _int_code(row.get("ASIIN_SERIAL")), "rc": trim_to_none(row.get("ASIOUT_FIELD_039")),
                        "error": error[:200] or None})
        return out

    def postings(self, account: str, window: Window, amount: Optional[Decimal] = None) -> list[Txn]:
        number = sql.digits(account_forms(account)[-1] if account_forms(account) else account)
        accounts = _account_list(account)
        clause = (f"\n  AND S.TRAN_AMOUNT BETWEEN {amount - Decimal('0.01')} AND {amount + Decimal('0.01')}"
                  if amount is not None else "")
        rows: list[dict[str, Any]] = []
        for year in range(window.start.year, window.end.year + 1):
            table = self.tran_pattern.replace("{year}", str(year))
            rows += self.mb.query(self.db, POSTINGS_SQL.replace("{table}", table)
                                  .replace("{accounts}", accounts)
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


def intellect_reversal(answers: list[dict[str, Any]]) -> dict[str, Any]:
    """From Intellect's answers for an RRN (CoreBanking.switch_answers): did the money Intellect took go back?
    reversed_at_intellect is "not needed" (it never reached Intellect, or Intellect did not approve it), "yes" (it
    approved the original and accepted the reversal) or "no" (it approved the original, and the reversal never
    reached it or was refused). Only "no" contradicts a reversal the switch reports."""
    originals = [a for a in answers if not a["mti"].startswith("04")]
    reversals = [a for a in answers if a["mti"].startswith("04")]
    if not originals:
        return {"reversed_at_intellect": "not needed", "why": "the transaction never reached core banking (Intellect)"}
    approved = [a for a in originals if a["rc"] == "00"]
    if not approved:
        codes = ", ".join(sorted({a["rc"] or "no reply" for a in originals}))
        return {"reversed_at_intellect": "not needed", "why": f"core banking (Intellect) did not approve it ({codes})"}
    original = approved[0]
    if any(r["rc"] == "00" for r in reversals):
        return {"reversed_at_intellect": "yes", "why": "core banking (Intellect) approved it and accepted the reversal"}
    if not reversals:
        return {"reversed_at_intellect": "no", "why": "core banking (Intellect) approved it and never received a "
                "reversal for it, so the client's debit stands"}
    reversal = reversals[0]
    why = (f"core banking (Intellect) approved it but refused the reversal (response {reversal['rc'] or 'none'}"
           + (f': "{reversal["error"]}"' if reversal["error"] else "") + ")")
    if reversal["branch"] == original["branch"] and None not in (reversal["serial"], original["serial"])             and reversal["serial"] < original["serial"]:
        why += ": the reversal reached it before the original, so it found nothing to reverse"
    return {"reversed_at_intellect": "no", "why": why + ", so the client's debit stands"}
