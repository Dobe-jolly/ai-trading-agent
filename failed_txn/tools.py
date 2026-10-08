"""The lookups Claude can call on a failed-transaction ticket, with masking, and the evidence they leave for the
rules in code.

Claude finds candidate transactions in each system, then pins the one the ticket is about with
confirm_transaction. Confirming checks core banking for the debit (and any refund) and gives the standard
outcome; the guardrails only let a ticket close on a confirmed transaction that belongs to the client.

Every account or reference the model passes must be on the ticket (form, thread, files read) or have come back
from an earlier lookup on this ticket, so it cannot go looking at other customers.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from agentcore import sql
from agentcore.metabase import MetabaseError
from agentcore.pii import Pseudonymizer
from .normalize import account_key, money, parse_amount, parse_day, reference_key
from .osticket import Details, Ticket, find_details
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
    InternetBanking,
    Postilion,
    Txn,
    Window,
    debit_check,
)

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 9000
MAX_SHOWN = 15

# Where a confirmed transaction leaves the client (the console's statuses)
FAILED_NO_DEBIT, FAILED_REFUNDED, NOT_FOUND = "failed_no_debit", "failed_refunded", "not_found"
CLOSABLE = {SUCCEEDED, DECLINED, REVERSED, FAILED_NO_DEBIT, FAILED_REFUNDED}

ACCOUNT_PROP = {"type": "string", "description": "The client's account number, e.g. <NUMBER_1>."}
DATE_PROP = {"type": "string", "description": "Day of the transaction, YYYY-MM-DD. Omit to search the recent past."}
AMOUNT_PROP = {"type": "string", "description": "Amount the client says moved, e.g. 20 or 1500.00 (no currency)."}
SWITCH_TOOL = {
    "name": "find_switch_transactions",
    "description": (
        "Postilion (the card and payments switch): ZIPIT, EcoCash bank-to-wallet and wallet-to-bank, POS and online "
        "purchases, ATM withdrawals, airtime bought with a card. Each result has the switch's response code and what "
        "it means, the reversal flag, RRN and STAN, and a reading worked out from those codes. Search by "
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
        "Core banking (standby copy): whether the account exists and its currency, and the debits and credits on it "
        "around a day (narration, batch). Use it to see whether the client was really debited and whether the money "
        "came back."),
    "input_schema": {"type": "object", "properties": {"account_number": ACCOUNT_PROP, "date": DATE_PROP,
                                                       "amount": AMOUNT_PROP}, "required": ["account_number"]},
}
CONFIRM_TOOL = {
    "name": "confirm_transaction",
    "description": (
        "Pin the transaction this ticket is about (an id from an earlier result, e.g. T3). It checks core banking for "
        "the debit and any refund, compares the amount with what the ticket says, and returns where that leaves the "
        "client with the standard outcome and wording. A ticket can only be closed on a confirmed transaction. Call it "
        "again with another id if you change your mind."),
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

    @property
    def subject(self) -> Optional[Txn]:
        return self.txns.get(self.subject_id) if self.subject_id else None

    @property
    def status(self) -> str:
        """Where the confirmed transaction leaves the client; not_found / none without one."""
        txn = self.subject
        if txn is None:
            searched = any(s["tool"] != "check_account_postings" for s in self.searches)
            return NOT_FOUND if searched and not [t for t in self.txns.values() if t.source != "core"] else "none"
        if txn.status == FAILED and self.debit:
            if self.debit.get("debited") == "no":
                return FAILED_NO_DEBIT
            if self.debit.get("refunded") == "yes":
                return FAILED_REFUNDED
        return txn.status

    @property
    def statuses(self) -> dict[str, str]:
        status = self.status
        return {"transaction": status} if status != "none" else {}

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


DECLINE_ADVICE = {
    "51": "There were not enough funds in the account at the time; the client can try again once the account is funded.",
    "61": "It exceeded the transaction limit; the client can try a smaller amount or ask the branch about the limit.",
    "65": "It exceeded the number of transactions allowed in the period; the client can try again later.",
    "55": "The PIN was incorrect; the client can try again with the correct PIN.",
    "75": "The PIN was entered wrongly too many times; the client should visit the branch to reset it.",
    "54": "The card has expired; the client should visit the branch for a new card.",
    "57": "The card is not allowed to make this kind of transaction; the client should ask the branch.",
}


def standard_reply(txn: Optional[Txn], debit: Optional[dict[str, Any]], status: str) -> tuple[str, str]:
    """(action, reply to the branch) for where the confirmed transaction leaves the client. Real values."""
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
        found = re.search(r"(?:response|code) (\w+)", txn.detail)
        advice = DECLINE_ADVICE.get(found.group(1) if found else "", "The client can try again.")
        reason = txn.detail.split("(", 1)[1].split(")", 1)[0] if "(" in txn.detail else txn.detail
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
                 lookback_days: int = 30, date_window_days: int = 3) -> None:
        self.postilion, self.connect, self.ib, self.core = postilion, connect, ib, core
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
        if self.core is not None:
            tools.append(POSTINGS_TOOL)
        tools.append(CONFIRM_TOOL)
        return tools

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            ticket: Optional[Ticket]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        if ticket is not None and not evidence.known:
            evidence.known.update(ticket.details())
        try:
            values = self._inputs(pseudo.unmask_obj(args or {}), evidence, ticket is not None)
            if name == "find_switch_transactions" and self.postilion is not None:
                data = self._switch(values, evidence)
            elif name == "find_nmb_connect_transactions" and self.connect is not None:
                data = self._connect(values, evidence)
            elif name == "find_internet_banking_transfers" and self.ib is not None:
                data = self._ib(values, evidence)
            elif name == "check_account_postings" and self.core is not None:
                data = self._postings(values, evidence)
            elif name == "confirm_transaction":
                data = self._confirm(str(args.get("transaction_id") or ""), evidence, ticket)
            else:
                return f"Unknown tool {name!r}.", True
        except (ValueError, sql.UnsafeValue) as exc:
            return f"Invalid input: {pseudo.mask(str(exc))}", True
        except MetabaseError as exc:
            evidence.check_errors.append(f"{name}: {exc}"[:300])
            return pseudo.mask(f"Lookup failed (data source error): {exc}. You may retry once.")[:800], True
        evidence.tools_used.append(name)
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
            evidence.searches.append({"tool": "check_account_postings", "args": {"account_number": values["account_number"]},
                                      "found": 0, "window": None})
            return {"account": account, "note": "This account number does not exist in core banking."}
        window = self._window(values.get("date")) if values.get("date") else Window.around(
            None, 0, min(self.lookback_days, 7), today=self.as_of)
        out = self._result("check_account_postings", values, self.core.postings(values["account_number"], window,
                                                                                values.get("amount")), evidence, window)
        out["account"] = account
        return out

    def _confirm(self, txn_id: str, evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        txn = evidence.txns.get(txn_id.strip().upper())
        if txn is None:
            raise ValueError(f"{txn_id!r} is not a transaction id from this ticket's results")
        evidence.subject_id = txn.id
        evidence.debit = None
        if self.core is not None and txn.source != "core" and txn.amount is not None:
            account = txn.account if txn.account and account_key(txn.account) in evidence.known.accounts else None
            if account is None and ticket is not None:
                account = next(iter(sorted(ticket.owner_details().accounts)), None)
            if account:
                day = txn.when.date() if txn.when else None
                window = Window.around(day, 1, 7, today=self.as_of)
                postings = self.core.postings(account, window)
                self._keep(postings, evidence)
                evidence.debit = debit_check(postings, txn.amount, day)
        owner = ticket.owner_details() if ticket is not None else Details()
        if not owner.amounts:
            evidence.amount_on_ticket = "none"
        else:
            evidence.amount_on_ticket = "yes" if any(txn.same_amount(a) for a in owner.amounts) else "no"
        status = evidence.status
        action, reply = standard_reply(txn, evidence.debit, status)
        out: dict[str, Any] = {"confirmed": txn.describe(), "where_this_leaves_the_client": status,
                               "amount_matches_ticket": evidence.amount_on_ticket,
                               "standard_outcome": {"action": action, "reply": reply}}
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
