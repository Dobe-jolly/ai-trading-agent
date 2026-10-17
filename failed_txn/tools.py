"""The lookups Claude can call on a failed-transaction ticket, with masking, and the evidence they leave for the
rules in code.

Claude finds candidate transactions in each system, then pins the one the ticket is about with
confirm_transaction. Confirming checks core banking for the debit (and any refund) and gives the standard
outcome; the guardrails only let a ticket close on a confirmed transaction that belongs to the client.

Every account or reference the model passes must be on the ticket (form, thread, files read) or have come back
from an earlier lookup on this ticket, so it cannot go looking at other customers.

Beyond the fixed lookups, query_data (agentcore.dataquery) lets Claude look into any approved table of the bank's
other databases, matched on the same identifiers; a ticket may also close on what such a query found for the ticket
owner's own account, mobile or reference.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from agentcore import sql
from agentcore import schemaquery
from agentcore.logs import tools as log_tools
from agentcore.dataquery import DataQuery, run_tool
from agentcore.metabase import MetabaseError
from agentcore.pii import Pseudonymizer
from .normalize import account_forms, account_key, mobile_key, money, parse_amount, parse_day, reference_key
from .osticket import Details, Ticket, cif_key, find_details
from .sources import (
    DECLINED,
    FAILED,
    KINDS,
    POSTED,
    REVERSAL_FAILED,
    REVERSAL_PENDING,
    REVERSED,
    SUCCEEDED,
    UNCERTAIN,
    Connect,
    CoreBanking,
    EcoCashLinks,
    InternetBanking,
    Postilion,
    Txn,
    Window,
    debit_check,
    describe_limit,
    intellect_reversal,
)

log = logging.getLogger(__name__)
FREE_FORM_TOOLS = ("query_data", "explore_data", "query_tables", "log_errors", "log_search")  # read tools whose inputs the agent composes
MAX_RESULT_CHARS = 9000
MAX_SHOWN = 15

# Where a confirmed transaction leaves the client (the console's statuses)
FAILED_NO_DEBIT, FAILED_REFUNDED, NOT_FOUND = "failed_no_debit", "failed_refunded", "not_found"
# Where a confirmed EcoCash link leaves the client, and the helpdesk's wording for those a ticket may close on
NOT_LINKED, LINKED_ELSEWHERE, LINKED_ACCOUNT_CLOSED = "not_linked", "linked_elsewhere", "linked_account_closed"
LINKED_OK, LINK_ACCOUNT_NOT_FOUND = "linked_ok", "account_not_found"
# Wallets linked to different accounts, or one fine and one not: the reply says what is seen and the branch decides
# which link is wrong (#426194, #752791). One account linked, but no account on the ticket to compare it with: a person.
LINKS_DIFFER, LINKED_NOTHING_TO_COMPARE = "links_differ", "linked_no_account_given"
LINK_CLOSABLE = {NOT_LINKED, LINKED_ELSEWHERE, LINKED_ACCOUNT_CLOSED, LINK_ACCOUNT_NOT_FOUND, LINKS_DIFFER}
TRANSACTION_SEARCHES = {"find_switch_transactions", "find_nmb_connect_transactions", "find_internet_banking_transfers"}
CLOSABLE = {SUCCEEDED, DECLINED, REVERSED, FAILED_NO_DEBIT, FAILED_REFUNDED}

ACCOUNT_PROP = {"type": "string", "description": "The client's account number, e.g. <NUMBER_1>."}
DATE_PROP = {"type": "string", "description": "Day of the transaction, YYYY-MM-DD. Omit to search the recent past."}
AMOUNT_PROP = {"type": "string", "description": "Amount the client says moved, e.g. 20 or 1500.00 (no currency)."}
SWITCH_TOOL = {
    "name": "find_switch_transactions",
    "description": (
        "Postilion (the card and payments switch): ZIPIT, EcoCash bank-to-wallet and wallet-to-bank, POS and online "
        "purchases, ATM withdrawals, airtime bought with a card. Each result has the switch's response code and what "
        "it means, the switch's own reason when it gave one (e.g. \"Sender's card not found\", an EcoCash error, "
        "Postilion's extended response code), the reversal flag, RRN and STAN, and a reading worked out from those "
        "codes. Search by "
        "account_number (with date and amount if known) or by reference (an RRN on the ticket or in a screenshot)."),
    "input_schema": {"type": "object", "properties": {
        "account_number": ACCOUNT_PROP, "reference": {"type": "string", "description": "RRN, e.g. <NUMBER_2>."},
        "date": DATE_PROP, "amount": AMOUNT_PROP}},
}
CONNECT_TOOL = {
    "name": "find_nmb_connect_transactions",
    "description": (
        "NMB Connect / USSD / app transactions and the billers they go to: RTGS, internal transfers, ZIPIT and "
        "Zimswitch, airtime and bundles (Econet, NetOne, Telecel), ZESA tokens, City of Harare, ZIMRA and other "
        "offline billers. Each result has the system's status (SUCCESS, FAILED, REVERSED, FLAG_REVERSAL, "
        "REVERSAL_FAILED, PENDING, ...), its code and message, the recipient, and a reading."),
    "input_schema": {"type": "object", "properties": {
        "account_number": ACCOUNT_PROP,
        "kind": {"type": "string", "enum": list(KINDS) + ["any"], "description": "What the client was paying for."},
        "date": DATE_PROP, "amount": AMOUNT_PROP}, "required": ["account_number"]},
}
IB_TOOL = {
    "name": "find_internet_banking_transfers",
    "description": "Internet banking RTGS and internal transfers to or from the account, with their status.",
    "input_schema": {"type": "object", "properties": {"account_number": ACCOUNT_PROP, "date": DATE_PROP,
                                                       "amount": AMOUNT_PROP}, "required": ["account_number"]},
}
POSTINGS_TOOL = {
    "name": "check_account_postings",
    "description": (
        "Core banking (standby copy): whether the account exists, its currency and status (ATM use enabled, dormant, "
        "inoperative, frozen, closed), and the debits and credits on it around a day (narration, batch). Use it to see whether the client was really debited and whether the money "
        "came back."),
    "input_schema": {"type": "object", "properties": {"account_number": ACCOUNT_PROP, "date": DATE_PROP,
                                                       "amount": AMOUNT_PROP}, "required": ["account_number"]},
}
CIF_TOOL = {
    "name": "find_client_accounts",
    "description": (
        "Core banking: the client's accounts for a CIF (customer number) on the ticket, with each account's currency "
        "and status (ATM use enabled, dormant, frozen, closed). Use it when the ticket gives a CIF, or only a short "
        "number in the subject, instead of an account number; then search the other lookups with the accounts it "
        "returns. If the number is not a CIF but an account number, it says so."),
    "input_schema": {"type": "object", "properties": {"cif": {"type": "string", "description": "The CIF from the ticket."}},
                     "required": ["cif"]},
}
ECOCASH_TOOL = {
    "name": "find_ecocash_links",
    "description": (
        "EcoCash bank-to-wallet / wallet-to-bank tickets only (not ZIPIT). EcoCash wallet links, from the bank's "
        "EcoCash registration log: which account a client's EcoCash mobile is "
        "linked to, in which currency and since when, or which mobiles an account is linked to, with each wallet's "
        "recent history (linked, linked by the earlier method, delinked). Use it for bank-to-wallet and "
        "wallet-to-bank problems: not linked, linked to another or old account, delink and register again, "
        "\"no universal account\". Give the account_number and/or the mobile from the ticket."),
    "input_schema": {"type": "object", "properties": {
        "account_number": ACCOUNT_PROP,
        "mobile": {"type": "string", "description": "The client's EcoCash mobile from the ticket, e.g. <MOBILE_1>."}}},
}
CONFIRM_LINK_TOOL = {
    "name": "confirm_ecocash_link",
    "description": (
        "EcoCash tickets only (not ZIPIT). When the ticket is about the EcoCash link itself (bank-to-wallet / "
        "wallet-to-bank not linked, linked to "
        "another or old account, registration or delinking), pin it: give the client's account_number and/or "
        "EcoCash mobile from the ticket. It reads the link log (as it stood on the ticket's day) and returns where "
        "the link leaves the client (not_linked, linked_elsewhere, linked_account_closed, account_not_found, "
        "links_differ, linked_ok, linked_no_account_given), what_we_see (each linked wallet, its account and that "
        "account's status) and a suggested outcome in the helpdesk's usual wording. A ticket about the link can be closed on not_linked, "
        "linked_elsewhere, linked_account_closed, account_not_found or links_differ (wallets linked to different "
        "accounts: say what you see; the branch decides which link is wrong)."),
    "input_schema": {"type": "object", "properties": {
        "account_number": ACCOUNT_PROP,
        "mobile": {"type": "string", "description": "The client's EcoCash mobile from the ticket, e.g. <MOBILE_1>."}}},
}
CONFIRM_TOOL = {
    "name": "confirm_transaction",
    "description": (
        "Pin the transaction this ticket is about (an id from an earlier result, e.g. T3). It checks core banking for "
        "the debit and any refund, and, for a switch transaction core banking declined, core banking's own error for "
        "it (e.g. a monthly limit exceeded); compares the amount with what the ticket says, and returns where that leaves the "
        "client, with a suggested outcome in the helpdesk's usual wording (a starting point: write the reply that fits "
        "the ticket). Call it again with another id if you change your mind."),
    "input_schema": {"type": "object", "properties": {"transaction_id": {"type": "string"}},
                     "required": ["transaction_id"]},
}


@dataclass
class Evidence:
    """What the lookups found, for the guardrails and the log."""
    txns: dict[str, Txn] = field(default_factory=dict)  # by id, T1, T2, ...
    searches: list[dict[str, Any]] = field(default_factory=list)
    known: Details = field(default_factory=Details)  # what the model may search with
    subject_id: Optional[str] = None
    debit: Optional[dict[str, Any]] = None  # core banking's answer for the subject
    amount_on_ticket: Optional[str] = None  # yes | no | none: does the subject's amount match the ticket?
    accounts_checked: dict[str, dict[str, Any]] = field(default_factory=dict)
    unread: list[str] = field(default_factory=list)  # tables that could not be read
    check_errors: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    client_accounts: set = field(default_factory=set)  # account keys found from a CIF the ticket owner gave
    link: Optional[dict[str, Any]] = None  # the confirmed EcoCash link: status, account, mobile, linked_account
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)  # query_data calls by query_id (Q1, Q2, ...)
    log_keys: dict[str, bool] = field(default_factory=dict)  # correlation ids seen in log results -> from the owner's identifier
    postings_checked: list[dict[str, Any]] = field(default_factory=list)  # check_account_postings: account, days, amount
    query_close: list[str] = field(default_factory=list)  # the queries a close rested on (set by the guardrails)

    @property
    def subject(self) -> Optional[Txn]:
        return self.txns.get(self.subject_id) if self.subject_id else None

    @property
    def status(self) -> str:
        """Where the confirmed transaction leaves the client; not_found / none without one."""
        txn = self.subject
        if txn is None:
            searched = any(s["tool"] in TRANSACTION_SEARCHES for s in self.searches)
            return NOT_FOUND if searched and not [t for t in self.txns.values() if t.source != "core"] else "none"
        if txn.status == FAILED and self.debit:
            if self.debit.get("debited") == "no":
                return FAILED_NO_DEBIT
            if self.debit.get("refunded") == "yes":
                return FAILED_REFUNDED
        if txn.status == REVERSED and self.debit and self.debit.get("debited") == "yes"                 and self.debit.get("refunded") == "no":
            return REVERSAL_FAILED  # the system says reversed, but core banking shows the debit and nothing back
        return txn.status

    @property
    def statuses(self) -> dict[str, str]:
        status = self.status
        out = {"transaction": status} if status != "none" else {}
        if self.link:
            out["ecocash_link"] = self.link["status"]
        return out

    def summary(self) -> dict[str, Any]:
        txn = self.subject
        return {"status": self.status, "confirmed": txn.id if txn else None, "found": len(self.txns),
                "debit": self.debit, "errors": self.check_errors[:3]}

    def same_amount_rivals(self) -> list[Txn]:
        """Other transactions of the subject's amount on the same day with a different reading."""
        txn = self.subject
        if txn is None or txn.when is None:
            return []
        return [t for t in self.txns.values() if t.id != txn.id and t.source == txn.source and t.same_amount(txn.amount)
                and t.when is not None and t.when.date() == txn.when.date() and t.status != txn.status]


def _when(txn: Txn) -> str:
    return txn.when.strftime("%d/%m/%Y %H:%M") if txn.when else "the day in question"


def _ref(txn: Txn) -> str:
    return f" (reference {txn.reference})" if txn.reference and txn.source != "core" else ""


# What the client can usually do after a decline, by response code (the general meaning; what the helpdesk knows
# beyond it, such as what to check on a 57 or which limit a 61 hit, is in the desk's staff notes: knowledge_seed.json).
DECLINE_ADVICE = {
    "51": "There were not enough funds in the account at the time; the client can try again once the account is funded.",
    "61": "It exceeded a transaction limit.",
    "65": "It exceeded the number of transactions allowed in the period; the client can try again later.",
    "55": "The PIN was incorrect; the client can try again with the correct PIN.",
    "75": "The PIN was entered wrongly too many times; the client should visit the branch to reset it.",
    "54": "The card has expired; the client should visit the branch for a new card.",
    "57": "The account is not permitted this kind of transaction.",
}


def decline_advice(txn: Txn) -> str:
    """The general advice for the response code, when no system gave a more specific reason (a specific reason, such
    as the switch's or core banking's own words, says more: the agent writes what to do about it)."""
    if txn.reason:
        return ""
    return DECLINE_ADVICE.get(decline_code(txn), "The client can try again.")


def decline_code(txn: Txn) -> str:
    found = txn.raw.get("rsp") or (re.search(r"(?:response|code) (\w+)", txn.detail) or [None, ""])[1]
    return str(found or "")


def mentions_amount(text: str, amount: Decimal) -> bool:
    """Whether a reply names this amount (250, 250.00, 1,500 or 1500.00 all count)."""
    whole, _, cents = f"{amount:.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ",?")
    tail = r"(?:\.00)?" if cents == "00" else rf"\.{cents}"
    return re.search(rf"(?<![\d.,]){grouped}{tail}(?![\d])", text or "") is not None


def link_reply(status: str, linked_account: Optional[str], seen: Optional[list[str]] = None) -> tuple[str, str]:
    """(action, reply to the branch) for where the confirmed EcoCash link leaves the client: the helpdesk's wording."""
    if status == NOT_LINKED:
        return ("reply_and_close", "Kindly advise the client to link the account for B2W or W2B services.")
    if status == LINKED_ELSEWHERE:
        return ("reply_and_close", f"Kindly note that the linked account is {linked_account}. Kindly delink that "
                "account and advise the client to register with the new account.")
    if status == LINKED_ACCOUNT_CLOSED:
        return ("reply_and_close", "Kindly advise the client to link the correct account: the linked account is closed.")
    if status == LINK_ACCOUNT_NOT_FOUND:
        return ("reply_and_close", f"Kindly advise the client to confirm the account number. It is failing to validate: "
                f"account {linked_account} does not exist in core banking.")
    if status == LINKS_DIFFER and seen:
        return ("reply_and_close", "Kindly note the EcoCash links we can see: " + "; ".join(seen) + ". Kindly delink "
                "the wrong link and advise the client to register with the correct account.")
    return ("escalate", "")


def _closed_by(wallet: dict[str, Any], as_of: Optional[date]) -> bool:
    closed = wallet.get("account_closed_on")
    return bool(closed) and (as_of is None or datetime.strptime(closed, "%d-%m-%Y").date() <= as_of)


def links_seen(wallets: list[dict[str, Any]], owner_accounts: set, as_of: Optional[date]) -> list[str]:
    """What the link log shows, one phrase per wallet linked now: mobile, currency, account, since when, and what
    matters about that account (the ticket's own, closed, dormant, ...). For the branch to decide which link is wrong."""
    out = []
    for w in wallets:
        if not w.get("linked_now"):
            continue
        notes = []
        if account_key(w.get("account")) in owner_accounts:
            notes.append("the account on this ticket")
        status = w.get("account_status")
        for note in ([status] if isinstance(status, str) else list(status or [])):
            if note == "open, no restrictions" or (note.startswith("the account was closed") and not _closed_by(w, as_of)):
                continue
            notes.append(note)
        out.append(f"{w.get('mobile')} ({w.get('currency')}) is linked to account {w.get('account')}"
                   + (f" ({'; '.join(notes)})" if notes else "") + (f" since {w['since']}" if w.get("since") else ""))
    return out


def link_status(wallets: list[dict[str, Any]], owner_accounts: set, as_of: Optional[date]) -> tuple[str, Optional[str]]:
    """(status, the account the wallet is linked to) from the wallets the lookup found. owner_accounts: the account
    keys the ticket gives (the account the client wants linked)."""
    verdicts = []
    for w in wallets:
        if not w.get("linked_now"):
            continue
        if _closed_by(w, as_of):
            verdicts.append((LINKED_ACCOUNT_CLOSED, w.get("account")))
        elif account_key(w.get("account")) in owner_accounts:
            verdicts.append((LINKED_OK, w.get("account")))
        elif owner_accounts:
            verdicts.append((LINKED_ELSEWHERE, w.get("account")))
        else:
            verdicts.append((LINKED_NOTHING_TO_COMPARE, w.get("account")))
    if not verdicts:
        return NOT_LINKED, None
    # One story for every wallet (same status, same account): that status. Otherwise the wallets differ, and the
    # reply says what is seen for each: no rule picks which one is wrong, the branch does.
    if len({v[0] for v in verdicts}) == 1 and len({account_key(v[1]) for v in verdicts}) == 1:
        return verdicts[0]
    return LINKS_DIFFER, None


def standard_reply(txn: Optional[Txn], debit: Optional[dict[str, Any]], status: str,
                   missing: Optional[list[Decimal]] = None, alike: int = 0) -> tuple[str, str]:
    """(action, reply to the branch) for where the confirmed transaction leaves the client. Real values.
    missing: the ticket's amounts when no attempt of them was found (a declined attempt of another amount is then
    described as such); alike: how many other attempts were declined with the same response code."""
    if txn is None:
        return ("request_info", "We could not find this transaction on the client's account. Kindly confirm the "
                "account number it was made from, the date and time, the amount and, if available, the reference "
                "(RRN) or a screenshot of the message the client received.")
    what = f"The {txn.channel} of {money(txn.amount, txn.currency)} on {_when(txn)}{_ref(txn)}"
    if status == SUCCEEDED:
        return ("reply_and_close", f"{what} went through successfully. If the recipient has not received it, kindly "
                "ask them to check with their bank or service provider" + (f", quoting reference {txn.reference}."
                                                                          if txn.reference else "."))
    if status == DECLINED:
        advice = decline_advice(txn)
        reason = txn.reason or (txn.detail.split("(", 1)[1].split(")", 1)[0] if "(" in txn.detail else txn.detail)
        if missing:
            asked = " or ".join(f"{a:,.2f}" for a in sorted(missing))
            others = (f" {alike} other attempt{'s' if alike > 1 else ''} on the account {'were' if alike > 1 else 'was'} "
                      f"declined with the same response code." if alike else "")
            return ("reply_and_close", f"We could not find an attempt of {asked} on the client's account. We can see "
                    f"the {txn.channel} of {money(txn.amount, txn.currency)} on {_when(txn)}{_ref(txn)}, which was "
                    f"declined ({reason}), and no money left the client's account.{others} {advice}")
        return ("reply_and_close", f"{what} was declined ({reason}) and no money left the client's account. {advice}")
    if status == REVERSED:
        return ("reply_and_close", f"{what} did not go through and was reversed, so the money is back in the client's "
                "account. Kindly ask the client to check the balance and try again if needed.")
    if status == FAILED_NO_DEBIT:
        return ("reply_and_close", f"{what} did not go through, and core banking shows no debit for it, so no money "
                "left the client's account. The client can try again.")
    if status == FAILED_REFUNDED:
        when = (debit or {}).get("refund_on") or "since"
        return ("reply_and_close", f"{what} did not go through. The client was debited, and the amount was credited "
                f"back on {when}. Kindly ask the client to check the balance.")
    return ("escalate", "")  # reversal pending or failed, uncertain, debited without refund: a person checks


class Toolbox:
    def __init__(self, postilion: Optional[Postilion] = None, connect: Optional[Connect] = None,
                 ib: Optional[InternetBanking] = None, core: Optional[CoreBanking] = None,
                 lookback_days: int = 30, date_window_days: int = 3, ecocash: Optional[EcoCashLinks] = None,
                 data: Optional[DataQuery] = None, schema: Optional[schemaquery.SchemaQuery] = None,
                 logs: Optional[log_tools.LogTools] = None) -> None:
        self.postilion, self.connect, self.ib, self.core = postilion, connect, ib, core
        self.schema = schema
        self.logs = logs
        self.ecocash = ecocash
        self.data = data if data is not None and data.sources else None
        self.lookback_days = lookback_days
        self.date_window_days = date_window_days
        self.as_of: Optional[date] = None  # closed-ticket replays: what was known then

    def schemas(self) -> list[dict[str, Any]]:
        tools = []
        if self.postilion is not None:
            tools.append(SWITCH_TOOL)
        if self.connect is not None:
            tools.append(CONNECT_TOOL)
        if self.ib is not None:
            tools.append(IB_TOOL)
        if self.ecocash is not None:
            tools.append(ECOCASH_TOOL)
            tools.append(CONFIRM_LINK_TOOL)
        if self.core is not None:
            tools.append(CIF_TOOL)
            tools.append(POSTINGS_TOOL)
        if self.data is not None:
            tools.append(self.data.tool_schema())
        if self.schema is not None:
            tools += self.schema.tool_schemas()
        if self.logs is not None:
            tools += self.logs.tool_schemas()
        tools.append(CONFIRM_TOOL)
        return tools

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            ticket: Optional[Ticket]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        if ticket is not None and not evidence.known:
            evidence.known.update(ticket.details())
        try:
            values = {} if name in FREE_FORM_TOOLS else self._inputs(pseudo.unmask_obj(args or {}), evidence,
                                                                  ticket is not None)
            if name == "query_data" and self.data is not None:
                data = self._query_data(pseudo.unmask_obj(args or {}), evidence, ticket)
            elif name in ("log_errors", "log_search") and self.logs is not None:
                data = self._logs(name, pseudo.unmask_obj(args or {}), evidence, ticket)
            elif name in ("explore_data", "query_tables") and self.schema is not None:
                data = self._query_tables(name, pseudo.unmask_obj(args or {}), evidence, ticket)
            elif name == "find_switch_transactions" and self.postilion is not None:
                data = self._switch(values, evidence)
            elif name == "find_nmb_connect_transactions" and self.connect is not None:
                data = self._connect(values, evidence)
            elif name == "find_internet_banking_transfers" and self.ib is not None:
                data = self._ib(values, evidence)
            elif name == "check_account_postings" and self.core is not None:
                data = self._postings(values, evidence)
            elif name == "find_client_accounts" and self.core is not None:
                data = self._client_accounts(values, evidence, ticket)
            elif name == "find_ecocash_links" and self.ecocash is not None:
                data = self._ecocash_links(values, evidence)
            elif name == "confirm_ecocash_link" and self.ecocash is not None:
                data = self._confirm_link(values, evidence, ticket)
            elif name == "confirm_transaction":
                data = self._confirm(str(args.get("transaction_id") or ""), evidence, ticket)
            else:
                return f"Unknown tool {name!r}.", True
        except (ValueError, sql.UnsafeValue) as exc:
            return f"Invalid input: {pseudo.mask(str(exc))}", True
        except sqlite3.Error as exc:  # the log store
            evidence.unread.append(f"{name}: {exc}"[:200])
            return f"The log store could not be read ({exc}).", True
        except MetabaseError as exc:
            if name in FREE_FORM_TOOLS:  # an exploratory read that failed does not hold up a close resting on others
                evidence.unread.append(f"{name} {str((args or {}).get('source') or (args or {}).get('database'))[:40]}: {exc}"[:200])
                return pseudo.mask(f"Query failed (data source error): {exc}. Check the columns and filters, or "
                                   "try another source. If it timed out, narrow the window and filter on an "
                                   "indexed column (explore_data lists them).")[:800], True
            evidence.check_errors.append(f"{name}: {exc}"[:300])
            return pseudo.mask(f"Lookup failed (data source error): {exc}. You may retry once.")[:800], True
        evidence.tools_used.append(name)
        if not data.pop("_no_follow_ups", False):  # counts over everyone's data never widen what may be searched
            evidence.known.update(find_details(json.dumps(data, default=str)))  # results may be searched further
        text = json.dumps(pseudo.mask_obj(data), default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
        return text, False

    # ---- inputs ------------------------------------------------------------------------------------
    def _inputs(self, args: dict[str, Any], evidence: Evidence, enforce: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        raw = str(args.get("account_number") or "").strip()
        if raw:
            self._token_check("account_number", raw)
            key = account_key(raw)
            if key is None:
                raise ValueError("account_number is not a valid account number")
            if enforce and key not in evidence.known.accounts:
                evidence.refused.append("account_number")
                raise ValueError("account_number is not on this ticket or in an earlier result; use the client's "
                                 "account from the ticket")
            out["account_number"] = raw
        raw = str(args.get("reference") or "").strip()
        if raw:
            self._token_check("reference", raw)
            key = reference_key(raw)
            if key is None:
                raise ValueError("reference must be 6 to 32 letters or digits")
            if enforce and key not in evidence.known.references and key.lstrip("0") not in evidence.known.accounts:
                evidence.refused.append("reference")
                raise ValueError("reference is not on this ticket or in an earlier result")
            out["reference"] = key
        if args.get("date"):
            day = parse_day(args["date"])
            if day is None:
                raise ValueError("date must be YYYY-MM-DD")
            out["date"] = day
        if args.get("amount") not in (None, ""):
            amount = parse_amount(args["amount"])
            if amount is None or amount <= 0:
                raise ValueError("amount must be a number such as 20 or 1500.00")
            out["amount"] = amount
        if args.get("kind"):
            out["kind"] = str(args["kind"])
        raw = str(args.get("mobile") or "").strip()
        if raw:
            self._token_check("mobile", raw)
            key = mobile_key(raw)
            if key is None:
                raise ValueError("mobile must be a Zimbabwe mobile number")
            if enforce and key not in evidence.known.mobiles:
                evidence.refused.append("mobile")
                raise ValueError("mobile is not on this ticket or in an earlier result")
            out["mobile"] = "0" + key
        raw = str(args.get("cif") or "").strip()
        if raw:
            self._token_check("cif", raw)
            key = cif_key(raw)
            if key is None or not raw.replace(" ", "").isdigit():
                raise ValueError("cif must be the customer number, 4 to 12 digits")
            if enforce and key not in evidence.known.cifs:
                evidence.refused.append("cif")
                raise ValueError("cif is not on this ticket; use the CIF the ticket gives")
            out["cif"] = key
        return out

    @staticmethod
    def _token_check(name: str, raw: str) -> None:
        if raw.startswith("<") and raw.endswith(">"):
            raise ValueError(f"{name}: {raw} is not a value from this ticket")

    def _window(self, day: Optional[date]) -> Window:
        return Window.around(day, self.date_window_days, self.lookback_days, today=self.as_of)

    def _keep(self, txns: list[Txn], evidence: Evidence) -> list[Txn]:
        """Number new transactions T1, T2, ... (the same transaction found twice keeps its first id)."""
        kept = []
        for txn in txns:
            twin = next((t for t in evidence.txns.values() if t.source == txn.source and t.when == txn.when
                         and t.amount == txn.amount and t.reference == txn.reference and t.status == txn.status), None)
            if twin is None:
                txn.id = f"T{len(evidence.txns) + 1}"
                evidence.txns[txn.id] = txn
                kept.append(txn)
            else:
                kept.append(twin)
        return kept

    def _result(self, tool: str, values: dict[str, Any], txns: list[Txn], evidence: Evidence, window: Optional[Window],
                unread: Optional[list[str]] = None) -> dict[str, Any]:
        kept = self._keep(txns, evidence)
        evidence.searches.append({"tool": tool, "args": {k: str(v) for k, v in values.items()}, "found": len(kept),
                                  "window": window.describe() if window else None})
        if unread:
            evidence.unread += unread
        counts: dict[str, int] = {}
        for t in kept:
            counts[t.status] = counts.get(t.status, 0) + 1
        out: dict[str, Any] = {"found": len(kept), "by_reading": counts,
                               "transactions": [t.describe() for t in kept[:MAX_SHOWN]]}
        if window:
            out["searched"] = window.describe() + (" (around the date given)" if values.get("date") else
                                                   f" (the last {self.lookback_days} days)")
        if values.get("amount") is not None:
            out["amount_filter"] = f"{values['amount']:,.2f}"
        if len(kept) > MAX_SHOWN:
            out["note"] = f"{len(kept) - MAX_SHOWN} older result(s) not shown; narrow with date or amount."
        if unread:
            out["could_not_read"] = unread
        return out

    # ---- lookups ------------------------------------------------------------------------------------
    def _switch(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.postilion is not None
        if values.get("reference"):
            return self._result("find_switch_transactions", values, self.postilion.by_reference(values["reference"]),
                                evidence, None)
        if not values.get("account_number"):
            raise ValueError("give account_number or reference")
        window = self._window(values.get("date"))
        txns = self.postilion.by_account(values["account_number"], window, values.get("amount"))
        return self._result("find_switch_transactions", values, txns, evidence, window)

    def _connect(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.connect is not None
        if not values.get("account_number"):
            raise ValueError("account_number is required")
        window = self._window(values.get("date"))
        txns, unread = self.connect.find(values["account_number"], values.get("kind", "any"), window, values.get("amount"))
        return self._result("find_nmb_connect_transactions", values, txns, evidence, window, unread)

    def _ib(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.ib is not None
        if not values.get("account_number"):
            raise ValueError("account_number is required")
        window = self._window(values.get("date"))
        return self._result("find_internet_banking_transfers", values,
                            self.ib.find(values["account_number"], window, values.get("amount")), evidence, window)

    def _postings(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.core is not None
        if not values.get("account_number"):
            raise ValueError("account_number is required")
        account = self.core.account(values["account_number"])
        evidence.accounts_checked[account_key(values["account_number"]) or ""] = account
        if not account["exists"]:
            evidence.postings_checked.append({"account": account_key(values["account_number"]), "exists": False})
            evidence.searches.append({"tool": "check_account_postings", "args": {"account_number": values["account_number"]},
                                      "found": 0, "window": None})
            return {"account": account, "note": "This account number does not exist in core banking."}
        window = self._window(values.get("date")) if values.get("date") else Window.around(
            None, 0, min(self.lookback_days, 7), today=self.as_of)
        out = self._result("check_account_postings", values, self.core.postings(values["account_number"], window,
                                                                                values.get("amount")), evidence, window)
        evidence.postings_checked.append({"account": account_key(values["account_number"]), "exists": True,
                                          "start": window.start, "end": window.end, "amount": values.get("amount")})
        out["account"] = account
        return out

    def _restrictions(self, account: Optional[str], evidence: Evidence) -> Optional[list[str]]:
        """The account's restrictions from core banking: a list (empty when there are none), or None when the account
        could not be checked."""
        if self.core is None or not account:
            return None
        try:
            found = self.core.account(account)
        except (MetabaseError, sql.UnsafeValue, ValueError) as exc:
            log.warning("Account status not read: %s", exc)
            return None
        evidence.accounts_checked[account_key(account) or ""] = found
        if not found.get("exists"):
            return None
        status = found.get("status") or {}
        if all(v is None for k, v in status.items() if k != "closed_on") and not status.get("closed_on"):
            return None  # flags not readable: do not report the account as clear
        return list(found.get("restrictions") or [])

    def _core_error(self, txn: Txn) -> Optional[Any]:
        """Intellect's own error for a switch transaction it did not approve (ASI interface log, by RRN). Read once per
        transaction; the first message becomes the transaction's reason. Not being able to read it does not block the
        ticket: the response code still stands."""
        if self.core is None or txn.source != "postilion" or txn.status not in (DECLINED, FAILED, UNCERTAIN) \
                or not txn.reference or txn.when is None:
            return None
        sink = str(txn.raw.get("sink") or "")
        if sink and "intellect" not in sink.lower():  # declined before it reached core banking
            return None
        if "core_errors" not in txn.raw:
            try:
                errors = self.core.switch_errors(txn.reference, txn.when.date())
            except (MetabaseError, sql.UnsafeValue) as exc:
                log.warning("Core banking error log not read for %s: %s", txn.id, exc)
                return "could not be read"
            txn.raw["core_errors"] = errors
            if errors:
                txn.reason = errors[0]
                txn.detail += f'; core banking says "{errors[0]}"'
        return txn.raw["core_errors"] or None

    def _intellect_reversal(self, txn: Txn) -> Optional[Any]:
        """For a switch transaction the switch reports reversed: did core banking (Intellect) accept the reversal? The
        switch's flag only says it sent one; a reversal Intellect refused, or one that reached it before the original,
        leaves the client debited. When Intellect approved the original and did not reverse it, the reading becomes
        reversal_failed (a person takes it). Read once per transaction; not being able to read it leaves the reading
        as it is (the core banking debit check still applies)."""
        if "intellect" in txn.raw:  # read already (the reading may have changed it to reversal_failed)
            return txn.raw["intellect"]
        if self.core is None or txn.source != "postilion" or txn.status != REVERSED or not txn.reference                 or txn.when is None:
            return None
        sink = str(txn.raw.get("sink") or "")
        if sink and "intellect" not in sink.lower():  # it never went to core banking
            return None
        if "intellect" not in txn.raw:
            try:
                answers = self.core.switch_answers(txn.reference, txn.when.date())
            except (MetabaseError, sql.UnsafeValue) as exc:
                log.warning("Core banking's answers not read for %s: %s", txn.id, exc)
                return "could not be read"
        reading = txn.raw["intellect"] = intellect_reversal(answers)
        if reading["reversed_at_intellect"] == "no":
            txn.status, txn.reversal = REVERSAL_FAILED, None
            txn.detail += f"; but {reading['why']}"
        return reading

    def _client_accounts(self, values: dict[str, Any], evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        assert self.core is not None
        cif = values.get("cif")
        if not cif:
            raise ValueError("cif is required")
        accounts = self.core.client_accounts(cif)
        owner_gave = ticket is None or cif in ticket.owner_details().cifs
        out: dict[str, Any] = {"cif": cif}
        if not accounts:  # branches sometimes put a short account number where a CIF would go
            account = self.core.account(cif)
            if account.get("exists"):
                accounts, out["note"] = [account], "This number is an account number, not a CIF."
            else:
                out["note"] = "No client with this CIF in core banking, and it is not an account number either."
        for a in accounts:
            key = account_key(a["account_number"])
            evidence.accounts_checked[key or ""] = a
            if owner_gave and key:
                evidence.client_accounts.add(key)
        evidence.searches.append({"tool": "find_client_accounts", "args": {"cif": cif}, "found": len(accounts),
                                  "window": None})
        out["accounts"] = accounts
        if accounts:
            out.setdefault("note", "Search the transaction lookups with these accounts (open ones first).")
        return out

    def _ecocash_links(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.ecocash is not None
        account, mobile = values.get("account_number"), values.get("mobile")
        if not account and not mobile:
            raise ValueError("give account_number or mobile")
        out = self.ecocash.find(account, mobile, as_of=self.as_of)
        # The status of each account a wallet is linked to now (a closed linked account is a common cause).
        if self.core is not None:
            for wallet in out["wallets"]:
                if wallet.get("account"):
                    try:
                        found = self.core.account(wallet["account"])
                    except (MetabaseError, sql.UnsafeValue, ValueError) as exc:
                        log.warning("Linked account status not read: %s", exc)
                        continue
                    wallet["account_status"] = ("does not exist in core banking" if not found.get("exists")
                                                else (found.get("restrictions") or ["open, no restrictions"]))
                    if found.get("exists"):
                        wallet["account_closed_on"] = (found.get("status") or {}).get("closed_on")
        evidence.searches.append({"tool": "find_ecocash_links", "args": {k: str(v) for k, v in values.items()},
                                  "found": len(out["wallets"]), "window": None})
        if not out["wallets"]:
            out["note"] = "No EcoCash link found for this " + ("account" if account and not mobile else "mobile"
                                                              if mobile and not account else "account or mobile") + "."
        return out

    def _confirm_link(self, values: dict[str, Any], evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        data = self._ecocash_links(values, evidence)
        owner = ticket.owner_details() if ticket is not None else Details()
        wanted = set(owner.accounts) | set(evidence.client_accounts)
        if values.get("account_number"):
            wanted.add(account_key(values["account_number"]))
        status, linked = link_status(data["wallets"], wanted - {None}, self.as_of)
        asked = values.get("account_number")
        if asked and self.core is not None:  # an account core banking does not know: nothing to link (#971899)
            try:
                if not self.core.account(asked).get("exists"):
                    status, linked = LINK_ACCOUNT_NOT_FOUND, asked
            except (MetabaseError, sql.UnsafeValue, ValueError) as exc:
                log.warning("Account not checked in core banking: %s", exc)
        evidence.link = {"status": status, "account": values.get("account_number"), "mobile": values.get("mobile"),
                         "linked_account": linked}
        seen = links_seen(data["wallets"], wanted - {None}, self.as_of)
        action, reply = link_reply(status, linked, seen)
        out: dict[str, Any] = {"link": data, "where_this_leaves_the_client": status, "what_we_see": seen,
                               "suggested_outcome": {"action": action, "reply": reply}}
        if status == LINKED_OK:
            out["note"] = ("The client's account is linked to the EcoCash mobile, so the link is not the problem: look "
                           "for the transaction instead.")
        elif status not in LINK_CLOSABLE:
            out["needs_a_person"] = True
        return out

    def _identifier_check(self, evidence: Evidence, ticket: Optional[Ticket], given: dict[str, Any], what: str,
                          field_name: str = "match_value"):
        """The `identify` callback of the free-form read tools: the value must be one this ticket gives (or an earlier
        result showed), checked as the other lookups check theirs, and is expanded to every form it may be stored in."""
        enforce = ticket is not None
        owner = ticket.owner_details() if ticket is not None else Details()

        def identify(kind: str, raw: str) -> list:
            self._token_check(field_name, raw)
            known = evidence.known
            if kind == "account":
                key, forms = account_key(raw), account_forms(raw)
                ok, mine = key in known.accounts, key in owner.accounts or key in evidence.client_accounts
            elif kind == "mobile":
                key = mobile_key(raw)
                forms = ["263" + key, "0" + key, key] if key else []
                ok, mine = key in known.mobiles, key in owner.mobiles
            elif kind == "reference":
                key = reference_key(raw)
                forms = sorted({raw.strip(), key}) if key else []
                ok = key in known.references or (key or "").lstrip("0") in known.accounts
                mine = key in owner.references or (key or "").lstrip("0") in owner.accounts
            else:
                key = cif_key(raw)
                forms = sorted({key, raw.strip()}) if key and raw.strip().isdigit() else []
                ok, mine = key in known.cifs, key in owner.cifs
            if key is None:
                raise ValueError(f"{field_name} is not a valid {kind}")
            if enforce and not ok:
                evidence.refused.append(f"{what} {kind}")
                raise ValueError(f"{field_name} ({kind}) is not on this ticket or in an earlier result")
            given.update(kind=kind, key=key, owner_given=bool(mine) or not enforce)
            return forms

        return identify

    def _query_data(self, request: dict[str, Any], evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        """query_data: one approved source, matched on an identifier this ticket gives (checked as the other lookups
        check theirs) or counts only. Recorded under a query_id a close may cite."""
        assert self.data is not None
        given: dict[str, Any] = {}
        identify = self._identifier_check(evidence, ticket, given, "query_data")
        out = run_tool(self.data, request, identify, evidence.queries, as_of=self.as_of)
        rec = evidence.queries[out["query_id"]]
        rec.update(key=given.get("key"), owner_given=bool(given.get("owner_given")))
        evidence.searches.append({"tool": "query_data", "args": {"source": rec["source"],
                                                                 "match_columns": rec["match_columns"]},
                                  "found": rec["rows"], "window": None})
        return out

    def _logs(self, name: str, request: dict[str, Any], evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        """log_errors (service-wide counts) and log_search (one client's lines, matched on an identifier this ticket gives
        or on a correlation id this ticket or an earlier log result showed). Recorded under a query_id."""
        assert self.logs is not None
        given: dict[str, Any] = {}
        identify = self._identifier_check(evidence, ticket, given, name, "identifier.value")
        ticket_text = ticket.all_text() if ticket is not None else ""

        def check_correlation(corr: str) -> None:
            if corr not in evidence.log_keys and corr not in ticket_text:
                evidence.refused.append("log_search correlation id")
                raise ValueError("correlation_id is not from this ticket or an earlier log result")
            given.update(owner_given=evidence.log_keys.get(corr, True))

        out = log_tools.run_tool(self.logs, name, request, identify, evidence.queries, check_correlation)
        rec = evidence.queries[out["query_id"]]
        if name == "log_search":
            rec.update(key=given.get("key"), owner_given=bool(given.get("owner_given")))
            for event in out.get("events", []):
                for pair in event.get("correlation", []):  # "requestId=abc": the value is what a later search uses
                    evidence.log_keys.setdefault(pair.partition("=")[2], bool(given.get("owner_given")))
        else:
            rec.update(key=None, owner_given=False)
        evidence.searches.append({"tool": name, "args": {"source": rec["source"], "match_columns": rec["match_columns"]},
                                  "found": rec["rows"], "window": None})
        return out

    def _query_tables(self, name: str, request: dict[str, Any], evidence: Evidence,
                      ticket: Optional[Ticket]) -> dict[str, Any]:
        """explore_data (names and types, no rows) and query_tables (up to three joined tables of one database,
        matched on an identifier this ticket gives, or counts only): recorded under a query_id a close may cite."""
        assert self.schema is not None
        given: dict[str, Any] = {}
        identify = self._identifier_check(evidence, ticket, given, "query_tables", "match.value")
        out = schemaquery.run_tool(self.schema, name, request, identify, evidence.queries, as_of=self.as_of)
        if name == "query_tables":
            rec = evidence.queries[out["query_id"]]
            rec.update(key=given.get("key"), owner_given=bool(given.get("owner_given")))
            evidence.searches.append({"tool": "query_tables", "args": {"source": rec["source"],
                                                                       "match_columns": rec["match_columns"]},
                                      "found": rec["rows"], "window": None})
        return out

    def _confirm(self, txn_id: str, evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        txn = evidence.txns.get(txn_id.strip().upper())
        if txn is None:
            raise ValueError(f"{txn_id!r} is not a transaction id from this ticket's results")
        evidence.subject_id = txn.id
        evidence.debit = None
        account = txn.account if txn.account and account_key(txn.account) in evidence.known.accounts else None
        if account is None and ticket is not None:
            account = next(iter(sorted(ticket.owner_details().accounts)), None)
        if account is None and evidence.client_accounts:
            account = sorted(evidence.client_accounts)[0]
        if self.core is not None and txn.source != "core" and txn.amount is not None:
            if account:
                day = txn.when.date() if txn.when else None
                window = Window.around(day, 1, 7, today=self.as_of)
                postings = self.core.postings(account, window)
                self._keep(postings, evidence)
                evidence.debit = debit_check(postings, txn.amount, day)
        core_error = self._core_error(txn)
        intellect = self._intellect_reversal(txn)
        # Response 57 that core banking's error log does not explain: the account's own flags.
        checked, permissions = False, None
        if txn.source == "postilion" and txn.status == DECLINED and decline_code(txn) == "57" \
                and not txn.raw.get("core_errors"):
            checked, permissions = True, self._restrictions(account, evidence)
            if permissions and not txn.reason:
                txn.reason = "; ".join(permissions)
        owner = ticket.owner_details() if ticket is not None else Details()
        if not owner.amounts:
            evidence.amount_on_ticket = "none"
        else:
            evidence.amount_on_ticket = "yes" if any(txn.same_amount(a) for a in owner.amounts) else "no"
        status = evidence.status
        missing = sorted(owner.amounts) if evidence.amount_on_ticket == "no" and status == DECLINED else None
        alike = sum(1 for t in evidence.txns.values() if t.id != txn.id and t.source == txn.source
                    and t.status == DECLINED and decline_code(t) == decline_code(txn)) if missing else 0
        action, reply = standard_reply(txn, evidence.debit, status, missing, alike)
        out: dict[str, Any] = {"confirmed": txn.describe(), "where_this_leaves_the_client": status,
                               "amount_matches_ticket": evidence.amount_on_ticket,
                               "suggested_outcome": {"action": action, "reply": reply}}
        if core_error:
            out["core_banking_error"] = core_error
        if intellect:
            out["core_banking_reversal"] = intellect
        if permissions:
            out["account_restrictions"] = permissions
        elif checked:  # what core banking showed, for the agent to reason on (the staff notes say what to do)
            out["account_restrictions"] = ("none found: ATM use enabled, not dormant, inoperative, frozen or closed"
                                           if permissions == [] else "could not be read")
        if txn.source == "postilion" and txn.status == DECLINED and decline_code(txn) in ("61", "65") \
                and not txn.raw.get("core_errors"):
            limits = txn.raw.get("limits") or []
            if limits:
                out["postilion_limits"] = [describe_limit(x, txn.currency) for x in limits]
            if txn.raw.get("limit_hit"):
                out["postilion_limit_hit"] = txn.raw["limit_hit"]  # the reason in the suggested reply
            else:
                out["postilion_limit_hit"] = ("none of Postilion's limits on the card clearly stopped it" if limits
                                              else "Postilion gave no limit details for it")
        if evidence.debit is not None:
            out["core_banking"] = evidence.debit
        elif txn.source != "core":
            out["core_banking"] = "not checked (core banking not available, or no account or amount)"
        rivals = evidence.same_amount_rivals()
        if rivals:
            out["caution"] = (f"{len(rivals)} other transaction(s) of the same amount that day read differently: "
                              + ", ".join(f"{t.id} {t.status}" for t in rivals) + ". Make sure this is the right one.")
        if status in (REVERSAL_PENDING, REVERSAL_FAILED, UNCERTAIN, POSTED, FAILED):
            out["needs_a_person"] = True
        return out
